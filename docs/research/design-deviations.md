# Design Deviations from Source Research

Living record of where the orchestrator implementation departs from its three source
documents. Update whenever implementation diverges further (owned by plan U10; see
docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md).

Sources:

- GENERAL_FLOW_MULTIAGENT.md (architecture research)
- IMPLEMENTATION_FLOW_MULTIAGENT.md (implementation research)
- CoCoder — https://github.com/Flitternie/CoCoder, analyzed in docs/research/cocoder-analysis.md

______________________________________________________________________

## vs CoCoder

| CoCoder does                                                                | We do                                                                                                                                                     | Why                                                                                                                                                                                                                            |
| --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| InfoMap community detection on repo file graphs (100+ nodes)                | networkx directed Louvain                                                                                                                                 | Task graphs are 7–20 nodes; InfoMap's flow-based machinery and native deps (infomap/igraph/leidenalg) pay off only at scale. Revisit via the partition strategy interface if group quality warrants (see Future improvements). |
| Edge weights from symbol-name cosine similarity                             | Edge weights from real codegraph relations (shared files, call proximity, impact overlap)                                                                 | Cosine was a proxy CoCoder needed because its code didn't exist at graph time (generate-from-spec benchmark). Real reference edges are strictly better ground truth.                                                           |
| `merge_small_groups` off by default (`ENABLE_MERGE_GROUPS` env var)         | Always on                                                                                                                                                 | Unbounded clustering output is the over-fragmentation failure this system exists to prevent. Both CoCoder guards kept (dependency direction, makespan no-regression).                                                          |
| `detect_roles()` labels in/out hubs inverted vs its own docstrings          | Ported by behavior with corrected names                                                                                                                   | Documented gotcha — cocoder-analysis.md §8 point 8.                                                                                                                                                                            |
| Partition computation fused with agent spawning in one tool                 | Separate compute-partition (dry-run inspectable) and execute steps                                                                                        | Human/review checkpoint on group boundaries before any session launches.                                                                                                                                                       |
| Tasks are files; no task→partition routing layer                            | LLM mapper routes plan tasks to code regions, verified against codegraph                                                                                  | Our input is a plan, not a whole-repo spec; CoCoder has no analog for this layer.                                                                                                                                              |
| OpenHands SDK agents, in-process message bus, LiteLLM                       | External Python orchestrator driving the `claude` CLI                                                                                                     | Different substrate entirely; only designs (scheduler state machine, watchdogs) carried over, no code.                                                                                                                         |
| No re-partitioning or spec rewrite on failure; leader patches code directly | Surprise-driven rewrite of unfinished groups and downstream dependencies                                                                                  | Origin requirement R12/R16; CoCoder's static partition is a benchmark simplification.                                                                                                                                          |
| No cycle detection (relies on deadlock monitor)                             | Explicit cycle detection at group-DAG build, fails loudly                                                                                                 | Cheap on small graphs; silent permanent-pending is unacceptable.                                                                                                                                                               |
| One directed weighted edge set (dependency edges carrying cosine weights)   | Two relations on the task graph: symmetric affinity (clustering weight) and directed dependencies (hub roles, lift, merge direction, makespan, group DAG) | Shared-file overlap has no direction; folding it into dependency edges would fabricate 2-cycles at the group level. Found during U1 implementation.                                                                            |
| No upper bound on community size (clustering output taken as-is)            | Over-budget groups split at the lowest-affinity boundary (reverse-Kruskal on affinity edges), recursing until every group fits                            | Origin AE2 requires every group under the token budget; CoCoder has no analog. Added in U1.                                                                                                                                    |
| Makespan no-regression guard carries merge safety                           | Guard ported, but with chain-compatibility checked first the greedy simulator cannot regress on an accepted merge — it is a belt-and-braces safety net    | Implementation finding (U1): a merge whose cross pairs are all dependency-ordered adds only serialization the dependency edges already imply. Kept because future strategies may relax the chain guard.                        |

## vs GENERAL_FLOW_MULTIAGENT.md

| Doc recommends                                                                      | We do                                                                                                             | Why                                                                                                                         |
| ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Base Context Compiler producing base head + per-service docs (`docs/services/*.md`) | Single compiled base-context document per run; per-service heads deferred                                         | v1 simplicity; fork-first delivery (below) covers the cache goal the doc's stable-prefix design aimed at.                   |
| Hybrid orchestrator (Claude Code agent for small graphs, external script for large) | Pure external Python always                                                                                       | Skills-only orchestration already failed in practice; determinism and zero orchestrator tokens won the brainstorm decision. |
| Group JSON schema with `estimatedTokens`, flat status enum                          | Adapted models: estimates live in the estimator, status extended (reviewing/rewriting/merging), generations added | Execution design (review loop, circuit breaker, merge stage) needs states the research schema didn't anticipate.            |
| Difficulty operationalized via structural + change-impact + verification metrics    | Kept, sourced from codegraph (fan-in/out, hub touches, impact overlap, verification count)                        | Same idea, concrete data source.                                                                                            |

## vs IMPLEMENTATION_FLOW_MULTIAGENT.md

| Doc recommends                                                               | We do                                                                                                                                                       | Why                                                                                                                                                                                                                                                                     |
| ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude --bg` background sessions per group, polled via `claude logs`/attach | Blocking `claude -p` rounds resuming the same session; process exit = round completion                                                                      | Deterministic completion detection, structured JSON usage output per round, no mid-turn-resume hazard. `--bg` unused in v1.                                                                                                                                             |
| Fresh sessions sharing a prefix via identical injected base document         | Fork-first: one base session per run, every worker/reviewer forked from it                                                                                  | Harness injections (git status, system reminders) differ across fresh sessions and break byte-identical prefixes; forking guarantees equality. Fallback to identical-head fresh sessions only if the U5 spike finds print-mode forking unusable — record it here if so. |
| Parse session IDs from CLI stdout                                            | Pre-assign via `--session-id`; record fork-observed IDs in the manifest                                                                                     | Manifest is the join contract either way; parsing stdout is fragile.                                                                                                                                                                                                    |
| Context-size threshold before reusing a session                              | Kept, plus round-count threshold, a generation cap (default 3), and generation tracking in the manifest                                                     | Rounds are the cheaper early signal; the cap prevents unbounded respawn loops; generations keep the analyzer join intact across respawns.                                                                                                                               |
| Structured JSON ferried through prompts in both directions                   | Orchestrator ferries control + artifact pointers only; reports/verdicts persist in the run dir; the reviewer pulls the diff itself from the shared worktree | MetaGPT-style blackboard/artifact patterns beat chat-ferrying on token efficiency, cache-stable prompt templates, and auditability (Perplexity research 2026-07-15).                                                                                                    |
| (not addressed) parallel session forking                                     | Fork calls serialized behind an orchestrator lock                                                                                                           | Claude Code's session store has no documented locking/atomicity; practitioner reports show shared-state races under concurrent instances (Perplexity research 2026-07-15). Forking is fast; serializing forks does not serialize group execution.                       |

______________________________________________________________________

## U5 spike findings (2026-07-16, claude CLI 2.1.211)

No deviation needed — every pinned mechanic works as designed:

- **Print-mode forking works and honors pre-assigned IDs.** `claude -p --resume <base-id> --fork-session --session-id <uuid>` returns the pre-assigned UUID as `session_id`, the
  fork inherits base-session context, and the base survives — repeated forks from the same
  base each produce their own transcript file. The identical-compiled-head fallback is not
  needed.
- **JSON envelope usage fields** (top level of `--output-format json`): `usage` with
  `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`
  (plus nested `cache_creation.ephemeral_1h_input_tokens`/`ephemeral_5m_input_tokens`),
  alongside `session_id`, `result`, `is_error`, `num_turns`, `total_cost_usd`,
  `stop_reason`, `permission_denials`, and per-model `modelUsage`. The breaker's
  context-size signal is the latest round's `input_tokens + cache_read_input_tokens + cache_creation_input_tokens + output_tokens` — `input_tokens` alone counts only
  non-cached input and grossly understates context.
- **`--json-schema` combines with `--resume`** — same session ID, structured JSON in
  `result`, no degraded mode needed for the speccer re-invocation path.
- **Transcript paths are deterministic:** `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
  where `<encoded-cwd>` is the worker's cwd with `/` → `-`. The manifest records them
  directly.

## Phase B implementation notes (2026-07-16)

- **Rewrite loops get their own bound.** The plan bounds respawns (generation cap) but
  not the rewriting cycle; a perpetual `too_hard` verdict would loop forever. Added
  `ExecutionConfig.max_rewrites` (default 2) — exceeding it fails the group like the
  generation cap does.
- **Every new coder session increments the generation counter** (breaker respawns and
  post-rewrite relaunches alike) so manifest session names stay unique; the *cap* is
  enforced only on breaker respawns, per the plan's wording.
- **Escalation rewrites carry context.** blocked/too_hard/structural/merge-conflict
  rewrites synthesize a `Surprise` describing the trigger so the speccer re-run sees why
  the spec failed, not just that it did.

## Phase C implementation notes (2026-07-16)

- **A group's diff base is the merge-base, not the captured tip.** U9's wiring captures
  the integration tip once per group at ready→running, but `base_ref_for` returns
  `git merge-base <tip> <group-branch>`: on a fresh launch that is the tip itself, and on
  `resume` (branch already exists) it is the branch's original fork point — the tip may
  have advanced past it via sibling merges, and diffing against the moved tip would
  under-report the group's work to its reviewer.
- **Rewritten specs are not persisted.** Spec rewrites live in run memory only; a
  `resume` relaunches groups from the specs in `groups.json`. Acceptable in v1 because a
  resumed group restarts its review loop anyway (its surprises re-fan-out if still
  relevant); revisit if resumed rewrites prove lossy in practice.
- **`failed` is terminal across resume — by design, with an operator escape hatch.**
  `resume` restarts only mid-flight groups; retrying a failed group means editing its
  entry in `state.json` back to `"ready"` (documented in orchestrator/README.md). An
  explicit `retry` command was deferred until real runs show it is wanted.
- **`--review-intensity` overrides every group uniformly.** Per-group overrides would
  need a selector syntax nobody has asked for yet; the flag exists for forcing paired
  review on a run you don't trust (or self_verify on one you do).
- **Stub growth for E2E (test infra, not product):** `tests/fake_claude.py` gained
  per-session script queues keyed by `--name` (bound at fork, followed by resumes) and
  scripted side effects (`files` writes + a real `git commit` in the worker's cwd) so
  merge scenarios exercise real git add/add conflicts. Deterministic conflict ordering
  uses a scripted `delay_s` on a *resume* round — never on a fork round, which would
  stall every sibling behind the runner's fork lock.

## Future improvements parked here

- **InfoMap / Leiden partition strategies** behind the strategy interface, if real-world
  group quality on larger plans (30+ tasks) shows Louvain boundaries degrading.
- **Fork-vs-compiled-head cache benchmark** — measure actual cache-hit rates and cost of
  both delivery mechanisms beyond the v1 spike.
- **`--bg` interactive worker mode** — if long-running groups ever need mid-round
  observation or steering.
- **Per-service context heads** (GENERAL_FLOW design) — if base-context docs grow past
  budget on large repos.
- **infinity-skills manifest ingestion** — `run_id`/`group_id`/`group_name` columns and a
  post-extract join pass (that repo; see docs/research/infinity-skills-analysis.md §2, §6).
- **Concurrent fork testing** — revisit fork-call serialization only if it ever becomes a
  measurable bottleneck; would require empirical load-testing of the session store since
  no official concurrency guarantees exist (research 2026-07-15).
