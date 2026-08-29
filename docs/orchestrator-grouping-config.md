# Configuration reference

Every knob in [`orchestrator/config.py`](../orchestrator/config.py), what it does,
which direction it moves the outcome, and how to reach it.

For *how grouping works*, read [`docs/orchestrator-grouping.md`](orchestrator-grouping.md) —
this file is the knob table, not the methodology.

______________________________________________________________________

## How a value is resolved

```
CLI flag  >  .orchestrator/config.toml  >  built-in default
```

- Defaults load with **no config file present** — the file is always optional
  ([`load_config`](../orchestrator/config.py), `config.py:204`).
- The conventional path is `<repo>/.orchestrator/config.toml`; override with
  `--config <path>`.
- CLI flags are layered on top by [`apply_overrides`](../orchestrator/cli.py)
  (`cli.py:272`). **Only the fields marked "CLI" below have a flag** — everything
  else is config-file-only.
- Unknown TOML keys are silently ignored by pydantic. One deprecated key is
  detected explicitly and warned about (`[session] timeout_s`, removed with the
  per-round timeout); the rest fail silently, so **typos in a config file are
  invisible** — check `group --no-spec` output against what you expected.

TOML section names match the model attribute names on `OrchestratorConfig`:

```toml
# .orchestrator/config.toml
[edge_weights]
call = 2.0

[partition]
hub_threshold = 0.4

[estimator]
token_budget = 100000

[difficulty]
d_review = 0.35

[breaker]
context_token_limit = 200000

[execution]
concurrency = 1

[session]
model = "claude-opus-5"

[escalation]
enabled = true
```

______________________________________________________________________

## `[edge_weights]` — how strongly each signal pulls tasks together

[`EdgeWeightsConfig`](../orchestrator/config.py), `config.py:18`. Consumed by
[`build_task_graph`](../orchestrator/grouping/graphing.py) (`graphing.py:230`).

These are **affinity** weights — they decide what clusters, not what runs first.

| Field            | Default | What it weights                                       | Raise it to…                                          | CLI |
| ---------------- | ------- | ----------------------------------------------------- | ----------------------------------------------------- | --- |
| `shared_file`    | 1.0     | one file two tasks both touch (real *or* prospective) | keep same-file work in one group                      | —   |
| `call`           | 2.0     | one caller/callee relation between two tasks' symbols | keep call-connected work together                     | —   |
| `impact`         | 1.5     | one `codegraph impact -d 2` overlap between two tasks | keep blast-radius-overlapping work together           | —   |
| `prose_neighbor` | 0.5     | a region-less task ↔ its plan-order neighbor          | pull unmappable tasks harder toward adjacent work     | —   |
| `semantic`       | 1.5     | one matched `implements`/`consumes` route tag         | strengthen cross-stack pairing                        | —   |
| `semantic_floor` | 0.5     | lower bound of the semantic layer's rescale           | give semantics more floor on greenfield plans         | —   |
| `semantic_ceil`  | 3.0     | upper bound of the semantic layer's rescale           | let semantics override structural edges on edit-heavy | —   |

`prose_neighbor` is not a codegraph signal — it is the only edge a task with no
files and no symbols ever gets, applied in
[`_with_prose_fallback`](../orchestrator/grouping/pipeline.py) (`pipeline.py:448`),
*after* `build_task_graph` returns.

**The semantic rescale.** The whole route-tag layer is multiplied by
`clamp(Σw_structural / Σw_semantic, semantic_floor, semantic_ceil)`
([`_add_semantic_layer`](../orchestrator/grouping/graphing.py), `graphing.py:341`).
The point is regime-independence: on a pure-greenfield plan the structural sum is
near zero, the ratio floors, and semantics dominate a near-empty layer; on an
edit-heavy plan the ratio hits the ceiling so semantics *refine* rather than
override real reference edges. Tune the bounds, not `semantic` alone — raising
`semantic` alone is largely cancelled by the rescale.

> ⚠️ `call` and `impact` currently feed **both** affinity and precedence. On a
> mature codebase the precedence half saturates into a cycle — see
> [Known limitations](orchestrator-grouping.md#known-limitations) in the
> methodology doc. Setting these to 0 removes the affinity too; it is not a
> workaround for the cycle problem.

______________________________________________________________________

## `[partition]` — clustering shape

[`PartitionConfig`](../orchestrator/config.py), `config.py:41`.

| Field                        | Default         | Effect                                                                                           | CLI                       |
| ---------------------------- | --------------- | ------------------------------------------------------------------------------------------------ | ------------------------- |
| `hub_threshold`              | 0.4             | fraction-of-all-tasks degree at which a node is reclassified from `core` to a hub                | —                         |
| `louvain_resolution`         | 1.0             | Louvain resolution γ — **higher = more, smaller communities**                                    | —                         |
| `allow_oversized_slice`      | `false`         | accept a declared slice that alone exceeds the budget cap, as one flagged group                  | `--allow-oversized-slice` |
| `allow_degenerate_partition` | `false`         | accept a partition whose cycle-repair left a group over the cap instead of a hard `GrouperError` | —                         |
| `granularity`                | `"independent"` | how eagerly `merge_small_groups` folds small groups together (plan U4)                           | `--granularity`           |

**`hub_threshold` is a fraction, not a count** — this surprises people. In a
10-task plan, a task with 4 upstreams crosses 0.4 and becomes a hub. Hub roles
([`detect_hub_roles`](../orchestrator/grouping/partition.py), `partition.py:355`)
have real consequences: a `utility_hub` is isolated into its own group and
scheduled first; every `aggregator_hub` is pooled into **one** trailing group.
Lower it → more hubs → more isolation and one fatter trailing group. Raise it →
almost everything is `core` and Louvain decides alone.

> If a `--no-spec` report shows *every* node as a hub, the dependency graph is
> saturated — the threshold is not the problem. See the methodology doc's
> limitation 4.

**`louvain_resolution` is the granularity dial that exists today.** γ > 1 splits
communities finer; γ < 1 merges them coarser. It is applied inside
[`_louvain`](../orchestrator/grouping/partition.py) (`partition.py:457`) with
`LOUVAIN_SEED = 42` (`partition.py:36`) so numbering is stable run to run.

**`allow_oversized_slice` and `--allow-oversized-slice` are exactly equivalent.**
Default behaviour is a hard `GrouperError` naming the slice, every member, each
member's work, the cap, and the overshoot
([`_check_slice_overflow`](../orchestrator/grouping/pipeline.py), `pipeline.py:98`).
The override keeps the slice whole as one over-cap group and records a flag.

**`granularity` and `--granularity` are exactly equivalent; the flag wins when
both are set (plan U4).** Three levels, each relaxing one more guard on
[`merge_small_groups`](../orchestrator/grouping/partition.py) (`partition.py:823`)
than the last — the budget cap, slice must-link, and cycle checks are never
relaxed at any level:

- `independent` (default) enforces both `chain_compatible` and the makespan
  no-regression check — today's behaviour, byte-for-byte, on every fixture in
  the register.
- `balanced` drops `chain_compatible` but still rejects a merge that would
  regress the simulated zero-communication makespan. In practice this is the
  level that actually changes anything: on every acyclic graph this
  partitioner produces, `chain_compatible` passing already implies the
  makespan check passes too (Kim & Browne 1988's admissibility test is a
  sufficient condition for Sarkar's), so relaxing the makespan check *alone*
  (independent → drop only Sarkar) is a no-op — see "Prior art and known
  limits of the dial" below.
- `monolithic` also drops the makespan check, collapsing as much as the hard
  guards allow.

See `tests/fixtures/grouping/granularity-ladder.md` for a worked shape where
`independent` → `balanced` → `monolithic` strictly reduces the group count, and
`grep -rn "granularity" tests/test_partition.py tests/test_grouping_fixtures.py`
for the tests pinning it.

______________________________________________________________________

## `[estimator]` — token pricing and the budget cap

[`EstimatorConfig`](../orchestrator/config.py), `config.py:62`. This is the only
group of knobs that changes **how many groups you get**, because the cap is what
`split_over_budget` enforces.

| Field                     | Default | Role                                                                | CLI              |
| ------------------------- | ------- | ------------------------------------------------------------------- | ---------------- |
| `token_budget`            | 100,000 | total context a single group's worker session may consume           | `--token-budget` |
| `bytes_per_token`         | 4.0     | source-bytes → tokens divisor                                       | —                |
| `slack_multiplier`        | 1.3     | headroom multiplier on everything derived from bytes                | —                |
| `per_file_tool_allowance` | 2,000   | flat tokens charged per file for tool output (reads, greps, edits)  | —                |
| `spec_tokens_allowance`   | 3,000   | partition-time stand-in for the group spec, which doesn't exist yet | —                |
| `size_hint_small`         | 500     | price of a prospective file declared `small` in `size_hints`        | —                |
| `size_hint_medium`        | 2,000   | …declared `medium` (equals the flat rate, by design)                | —                |
| `size_hint_large`         | 5,000   | …declared `large`                                                   | —                |

### The two formulas

**Per-task work** ([`node_work`](../orchestrator/grouping/estimator.py), `estimator.py:44`) —
what the partitioner sums per group:

```
work = source_bytes / bytes_per_token * slack_multiplier
     + len(existing_files)     * per_file_tool_allowance
     + Σ over prospective files: size_hints[f] ? size_hint_<class> : per_file_tool_allowance
```

**Budget cap** ([`partition_budget_cap`](../orchestrator/grouping/estimator.py), `estimator.py:70`) —
what that sum must stay under:

```
head = (base_tokens + spec_tokens_allowance) * slack_multiplier
cap  = max(token_budget - head, 0)
```

`base_tokens` is the compiled base context (conventions + codegraph summary + the
plan) measured at grouping time, so **the cap moves when your plan or CLAUDE.md
grows**. In this repo, base ≈ 9,300 tokens → head ≈ 16,000 → cap ≈ **84,000**.

A separate, looser check runs on the *final* group after specs exist
([`estimate_group_tokens`](../orchestrator/grouping/estimator.py), `estimator.py:19`,
gated by `is_over_budget` at `:32`) and only appends a flag.

### Which knob to reach for

| You want                     | Change                                              | Why                                                                                    |
| ---------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------------- |
| **fewer, larger groups**     | raise `token_budget`                                | directly raises the cap; the most predictable dial                                     |
| **more, smaller groups**     | lower `token_budget`, or raise `louvain_resolution` | lowering the cap forces more splits                                                    |
| honest pricing of tiny files | declare `size_hints` in the task map                | a flat rate charges `tsconfig.json` like a 400-line module                             |
| slice integrity              | **none of these**                                   | measured: sweeping `per_file_tool_allowance` 2000→100 never restored a dissolved slice |

> Pricing buys **precision, not integrity.** The recorded sweep is in the
> methodology doc — lowering the per-file rate *dissolved* a slice that had
> survived, because cheaper nodes let `merge_small_groups` build bigger clusters
> that then breach the cap and get cut.

______________________________________________________________________

## `[difficulty]` — which groups get a reviewer

[`DifficultyConfig`](../orchestrator/config.py), `config.py:78`. Affects **cost at
`run` time**, never group boundaries.

Each signal is normalized as `x / (x + scale)` — so `scale` is *the raw value at
which that signal contributes half its weight*. The weighted sum lands in \[0, 1)
([`difficulty_score`](../orchestrator/grouping/estimator.py), `estimator.py:99`).

| Signal             | Weight (default)                | Scale (default)                | Raw meaning                                      |
| ------------------ | ------------------------------- | ------------------------------ | ------------------------------------------------ |
| files touched      | `weight_files_touched` 1.0      | `scale_files_touched` 6.0      | files in the group's union                       |
| max fan            | `weight_max_fan` 1.5            | `scale_max_fan` 10.0           | largest caller/callee count of any mapped symbol |
| hub touches        | `weight_hub_touches` 2.0        | `scale_hub_touches` 1.0        | member tasks whose role isn't `core`             |
| cross-group edges  | `weight_cross_group_edges` 1.5  | `scale_cross_group_edges` 3.0  | dependency edges crossing the group boundary     |
| verification items | `weight_verification_items` 1.0 | `scale_verification_items` 5.0 | verification bullets the speccer wrote           |

Tiers ([`intensity_for`](../orchestrator/grouping/estimator.py), `estimator.py:126`):

| Field      | Default | Meaning                                                      |
| ---------- | ------- | ------------------------------------------------------------ |
| `d_review` | 0.35    | below → `self_verify` (**no reviewer session at all**)       |
| `d_hard`   | 0.65    | below → `paired` (one reviewer); at or above → `paired_plus` |

Lower `d_review` → more groups get a reviewer → more tokens, better catch rate.
`run --review-intensity <tier>` overrides the computed tier for **every** group.

> Note the interaction with the cycle problem: a saturated dependency graph makes
> every node a hub, which inflates `hub_touches` and `cross_group_edges` and
> pushes everything toward `paired_plus`. Conversely, dropping `symbols` from a
> task map to dodge the cycle zeroes `max_fan` and lands everything on
> `self_verify`.

______________________________________________________________________

## `[breaker]` — when a worker is retired mid-run

[`BreakerConfig`](../orchestrator/config.py), `config.py:103`. Execution-time only.

| Field                       | Default | Effect                                                                     |
| --------------------------- | ------- | -------------------------------------------------------------------------- |
| `context_token_limit`       | 200,000 | context occupancy at which the current coder session is retired and forked |
| `max_rounds_per_generation` | 3       | review rounds one coder generation may run before a fork                   |
| `max_generations`           | 3       | coder generations before the group is failed                               |

`context_token_limit` is read against the **last iteration's** usage, not the sum
of all turns — summing inflated it ~50× and retired healthy coders. Keep it
comfortably under the model's real window; the gap absorbs one more round.

______________________________________________________________________

## `[execution]` — parallelism and worker permissions

[`ExecutionConfig`](../orchestrator/config.py), `config.py:117`.

| Field                           | Default       | Effect                                                                                                                                | CLI                 |
| ------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| `concurrency`                   | 1             | max groups running at once                                                                                                            | `--concurrency N`   |
| `sequential`                    | `false`       | deterministic one-at-a-time debug mode                                                                                                | `--sequential`      |
| `permission_mode`               | `acceptEdits` | permission mode passed to each worker's `claude` CLI                                                                                  | `--permission-mode` |
| `max_rewrites`                  | 2             | spec rewrites for a stuck group before it fails                                                                                       | —                   |
| `max_conflict_resolve_attempts` | 1             | warm-resume attempts at the group's own coder session to resolve a merge conflict in place before falling back to a full spec rewrite | —                   |

**Serial is the default on purpose.** Each group's worktree is cut from the
integration tip at its ready→running transition, so one-at-a-time stacks each
group on the previous one's merged work: no cross-group merge conflicts, and a
usage-limit hit costs at most one in-flight group.

**Above `concurrency = 1`, file overlap excludes.** Two groups declaring a file
in common are never both running, even when the group DAG leaves them unordered.
This has no config field and cannot be turned off — it is a scheduler invariant,
not a policy, because a run at `--concurrency 4` has no other defence against two
groups editing `cli.py` at once and colliding at merge. The relation is symmetric:
whichever group is admitted first holds the other for as long as it is active, no
dependency edge is created, and either order is correct. `status` names the hold
and the shared files (`held (file_overlap) by g1 on cli.py`), and `run.log`
records it once when it starts.

The exclusion keys off each group's **declared** files, so it covers files a group
plans to create as well as ones it edits — but it cannot see an *undeclared*
collision. Two groups that both write a file neither declared still conflict at
merge, and the conflict→rewrite path remains the net that catches them.

> ⚠️ `acceptEdits` covers **edits, not Bash.** Orchestrated coders cannot run
> `pytest` or `git commit` unless an approver is wired up — a large hidden cost
> driver, since a coder that cannot verify burns rounds guessing.

______________________________________________________________________

## `[preflight]` — the mechanical, LLM-free merge gate

[`PreflightConfig`](../orchestrator/config.py). Runs before every merge attempt:
worktree clean, then every resolved **check step** exits zero. A failure is
classified into one of `env` / `timeout` / `regression` before it is ever routed
(see the reference HTML's Preflight & auth recovery section) — only
`regression`, and only when it introduces nothing the launch-branch baseline did
not already have, actually spends a rewrite.

### A check run is a sequence of steps, not one command

It used to be one command, resolved by marker *precedence*: `pyproject.toml` /
`uv.lock` won, and `package.json` was reached only when there were no uv
markers. In a repo carrying both — this one — every merge was gated on
`uv run pytest` alone and no group's JavaScript was ever compiled or tested.
`detect_check_steps` now resolves all of them, run in order, first failure
stopping the run:

| Step         | When it is detected                                              | Command                                                        | JUnit |
| ------------ | ---------------------------------------------------------------- | -------------------------------------------------------------- | ----- |
| `pytest`     | `pyproject.toml` or `uv.lock` at the root                        | `uv run pytest -p no:cacheprovider --junitxml=<out>`            | yes   |
| `npm-test`   | root `package.json`, **no** uv markers                           | `npm test`                                                      | no    |
| `vitest`     | `ui/package.json` + `ui/node_modules` + `vitest` in devDeps      | `npx vitest run --reporter=junit --outputFile=<out>` in `ui/`   | yes   |
| `npm-test-ui`| as above but no `vitest` devDep                                  | `npm test` in `ui/`                                             | no    |
| `tsc`        | `typescript` in `ui/` devDeps + `ui/tsconfig.json`               | `npx tsc --noEmit` in `ui/`                                     | no    |

vitest's JUnit reporter emits the same `<testcase classname=… name=…>` shape
pytest's does, so one parser serves both; vitest ids are namespaced `ui::` so
the two id spaces cannot collide. The UI gate costs about 9s per merge on this
repo (199 vitest tests in ~6s, `tsc` in ~3s).

`ui/node_modules` is provisioned by `provision_node_env` (`npm ci`) alongside
`uv sync`, for group and integration worktrees alike. When it is **missing the
UI steps are skipped, never failed** — an `env`-kind preflight failure raises
`GroupFailure`, which under `--on-failure halt` kills the run, and a machine
without npm must not be able to do that. The asymmetry is logged instead:
`preflight: step 'vitest' was in the baseline but is skipped here (no ui/node_modules)`.

### When a failure is excused

The baseline (`preflight-baseline.json`, captured once on the launch branch)
records **every step**: its exit code, and its per-test outcomes where it wrote
a report. A failing step is excused as pre-existing only against *comparable
evidence*:

- a step that writes JUnit — compared by failing-test set, unioned across steps
  with each step's id prefix applied. An **empty** set is no evidence, not
  "nothing new": `∅ - baseline` is `∅`, which reads as pre-existing against
  every baseline, and that is exactly how a report-less regression once merged
  silently.
- a step that writes none (`tsc`, a bare `npm test`) — compared by exit code
  against the baseline's record of *that same step name*. Red at launch and red
  here introduced nothing; red here against a clean launch is a hard block.

`env` and `timeout` failures are never excused: neither produced a comparable
result at all.

pytest's exit-code table (1 = tests failed, 2/3/4/5 = the run never really
happened) is applied only to the `pytest` step and to an explicitly configured
`check_command`. Any other step's nonzero exit is a `regression`: `tsc` exits
**2** on an ordinary type error, and reading that through pytest's table would
call it `env` — unattributable, so no rewrite for the coder, and a halted run.

| Field             | Default | Effect                                                                                                                        | CLI |
| ----------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------- | --- |
| `check_command`   | `null`  | overrides detection with a single step; gets `--junitxml` appended when it ends in `pytest`. `null` and no markers means no check runs at all | —   |
| `check_timeout_s` | `900.0` | applied per step; a hung one holds the merge lock for every other group — always a `timeout`-kind failure, never a silent pass | —   |

## `[auth]` — the auth-refresh ladder's pause rung

[`AuthConfig`](../orchestrator/config.py). Rungs (a) and (b) of the ladder — read
`expiresAt`, refresh in place — run once, synchronously, the moment a 401 is
seen and need no timing config. This section covers only rung (c), what happens
when both fail.

| Field              | Default | Effect                                                                   | CLI |
| ------------------ | ------- | ------------------------------------------------------------------------ | --- |
| `enabled`          | `true`  | whether the ladder is wired in at all                                    | —   |
| `credentials_path` | `null`  | overrides `~/.claude/.credentials.json` (chiefly for tests)              | —   |
| `poll_s`           | `60.0`  | how often the armed pause re-probes `AuthLadder.recover()`               | —   |
| `max_wait_s`       | `0.0`   | total time to wait before giving up; `0` = wait indefinitely             | —   |
| `max_attempts`     | `6`     | retries of the same call across pauses before the pause is unrecoverable | —   |

`[auth]` is deliberately its own config model rather than reusing
`[usage_limit]` wholesale — an auth pause has no reset-time prose to parse (there
is never a deadline, only "healthy again"), so a skew field would be dead here.

______________________________________________________________________

## `[session]` — how the `claude` CLI is shelled

[`SessionConfig`](../orchestrator/config.py), `config.py:137`.

| Field                 | Default             | CLI               | Effect                                                                                                                                                             |
| --------------------- | ------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `claude_bin`          | `"claude"`          | —                 | binary to shell; accepts a list so tests point it at a stub interpreter                                                                                            |
| `model`               | `"claude-sonnet-5"` | `--model-worker`  | model for coder/reviewer worker forks — the bulk of a run's spend, and mostly mechanical work                                                                      |
| `base_model`          | `"claude-opus-5"`   | `--model-base`    | model for the run's own base session — only reached under `fork_base_session`; with the default off, no base session is ever created                               |
| `fork_base_session`   | `false`             | `--fork-base` / `--no-fork-base` | legacy: launch workers by forking the run's base session. The fork misses that session's prompt cache (19,968 tokens hit, ~41.5k re-created) because the cache key embeds the cwd and each group's cwd is its own worktree — see [ADR 0007](adr/0007-workers-start-fresh-instead-of-forking-the-base-session.md) |
| `speccer_model`       | `"claude-opus-5"`   | `--model-speccer` | model for the mapper/speccer's `claude -p` calls — one call per grouping (or per spec rewrite), where the strongest model earns its cost                           |
| `allowed_tools`       | `[]`                | —                 | extra `--allowedTools` entries                                                                                                                                     |
| `transcript_root`     | `null`              | —                 | override `~/.claude/projects` (tests)                                                                                                                              |
| `max_thinking_tokens` | `4000`              | —                 | `--max-thinking-tokens` per worker turn; thinking counts as *output* tokens, a real cost driver — raise per-run in config.toml when a group needs deeper reasoning |
| `thinking`            | `"adaptive"`        | —                 | `--thinking` mode: `enabled` (always) / `adaptive` (model decides) / `disabled` (never); orthogonal to the token budget above                                      |

By default every worker is a fresh session whose first prompt carries the
compiled base context, and the run spawns no base session at all — so
`manifest.base_session_id` is `null` and `base_model` goes unused (ADR 0007).

The three model fields are independently settable (plan U17): workers default to
the cheaper Sonnet model, while the base session and the speccer default to Opus.
CLI flags win over the config file, which wins over these defaults; the run
banner prints all three resolved model ids.

`[session] timeout_s` is **removed**. A config still carrying it gets an explicit
stderr warning (`config.py:220`) instead of being silently dropped.

______________________________________________________________________

## `[escalation]` — human-in-the-loop

[`EscalationConfig`](../orchestrator/config.py), `config.py:168`. **On by
default** at `on_stuck`; an unattended `run` needs `--intensity autonomous` (or
`enabled = false`) to stay fully autonomous, otherwise it can block indefinitely
waiting on an unanswered escalation.

| Field             | Default                    | Effect                                                              | CLI                                 |
| ----------------- | -------------------------- | ------------------------------------------------------------------- | ----------------------------------- |
| `enabled`         | `true`                     | master switch                                                       | `--intensity autonomous` to disable |
| `intensity`       | `on_stuck`                 | which moments pause for the operator                                | `--intensity`                       |
| `source`          | `workers_via_orchestrator` | whether a coder's `needs_input` reaches the operator                | `--escalation-source`               |
| `timeout_s`       | `null` (block forever)     | seconds to wait for an answer                                       | `--escalation-timeout`              |
| `on_timeout`      | `autonomous`               | fallback when an escalation times out (`autonomous`/`skip`/`abort`) | —                                   |
| `poll_interval_s` | 1.0                        | broker poll interval                                                | —                                   |

Tiers, increasing: `autonomous` < `on_failure` < `on_stuck` < `interactive`.

Flag interactions in [`apply_overrides`](../orchestrator/cli.py) (`cli.py:233`):
`--hitl` enables; any non-`autonomous` `--intensity` **implies** `--hitl`;
`--intensity autonomous` **forces it off**, even over a config file that enabled it.

______________________________________________________________________

## CLI flags with no config field

Grouping-time behaviour reachable only from the command line:

| Flag                        | Command | Effect                                                                                      |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------- |
| `--name <tag>`              | `group` | grouping directory name (default: the plan's filename stem)                                 |
| `--dry-run`                 | `group` | print the report, write only `grouping-trace.json`                                          |
| `--no-spec`                 | `group` | partition-only report, **zero LLM calls**; never writes `groups.json`                       |
| `--allow-unknown-symbols`   | `group` | drop task-map symbols missing from the codegraph index with a flag instead of hard-erroring |
| `--repo <path>`             | all     | target repo root (default: cwd)                                                             |
| `--config <path>`           | all     | config TOML path (default: `<repo>/.orchestrator/config.toml`)                              |
| `--grouping <tag>`          | `run`   | which named grouping to execute                                                             |
| `--run-id <id>`             | `run`   | run identifier (default: `r<timestamp>`)                                                    |
| `--review-intensity <tier>` | `run`   | override the computed review tier for **every** group                                       |

`--no-spec` is the cheap iteration loop: it exercises the entire deterministic
core and writes the trace, at zero token cost.

______________________________________________________________________

## Tuning recipes

**"I want fewer, larger groups."** Raise `[estimator] token_budget`. It raises the
cap linearly and is the only knob whose effect is monotone and predictable.

**"I want more, smaller groups."** Lower `token_budget` first. If the shape is
still wrong, raise `[partition] louvain_resolution` above 1.0 — but note this
changes *which* tasks cluster, not just how many groups there are.

**"One group is enormous and everything else is tiny."** Look at hub roles in the
`--no-spec` report before touching weights. A single `aggregator_hub` bucket is
the usual cause; the fix is usually the plan's `depends_on`, not `hub_threshold`.

**"My slice got split."** Not a pricing problem — see the sweep in the methodology
doc. Check whether the slice's own summed work exceeds the cap (the error names
every member and its work).

**"Grouping collapsed to one group."** Not a config problem. See
[Known limitations](orchestrator-grouping.md#known-limitations) limitation 4.

______________________________________________________________________

## Field inventory

Every field in `orchestrator/config.py`, for cross-checking that this reference
stays complete. 11 models, 65 fields — `usage_limit` is a real config section
([`UsageLimitConfig`](../orchestrator/config.py)) not yet given its own `##`
section above; listed here so the inventory stays complete regardless.

| Section        | Fields                                                                                                                                                                                                                                                            |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `edge_weights` | `shared_file`, `call`, `impact`, `prose_neighbor`, `semantic`, `semantic_floor`, `semantic_ceil`                                                                                                                                                                  |
| `partition`    | `hub_threshold`, `louvain_resolution`, `allow_oversized_slice`, `allow_degenerate_partition`, `granularity`                                                                                                                                                       |
| `estimator`    | `token_budget`, `bytes_per_token`, `slack_multiplier`, `per_file_tool_allowance`, `spec_tokens_allowance`, `size_hint_small`, `size_hint_medium`, `size_hint_large`                                                                                               |
| `difficulty`   | `weight_files_touched`, `weight_max_fan`, `weight_hub_touches`, `weight_cross_group_edges`, `weight_verification_items`, `scale_files_touched`, `scale_max_fan`, `scale_hub_touches`, `scale_cross_group_edges`, `scale_verification_items`, `d_review`, `d_hard` |
| `breaker`      | `context_token_limit`, `max_rounds_per_generation`, `max_generations`                                                                                                                                                                                             |
| `execution`    | `concurrency`, `sequential`, `permission_mode`, `max_rewrites`, `max_conflict_resolve_attempts`                                                                                                                                                                   |
| `preflight`    | `check_command`, `check_timeout_s`                                                                                                                                                                                                                                |
| `usage_limit`  | `auto_resume`, `max_wait_s`, `max_attempts`, `skew_s`                                                                                                                                                                                                             |
| `auth`         | `enabled`, `credentials_path`, `poll_s`, `max_wait_s`, `max_attempts`                                                                                                                                                                                             |
| `session`      | `claude_bin`, `model`, `base_model`, `speccer_model`, `allowed_tools`, `transcript_root`, `max_thinking_tokens`, `thinking`                                                                                                                                       |
| `escalation`   | `enabled`, `intensity`, `source`, `timeout_s`, `on_timeout`, `poll_interval_s`                                                                                                                                                                                    |
