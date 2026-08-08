---
date: 2026-07-22
topic: orchestrator-run-hardening
---

# Orchestrator Run Hardening — Requirements

## Summary

Fix every serious, non-grouping defect in `docs/orchestrators_improvements.md` in
one scope: envelope failures become automatically recoverable with warm session
resume (D9), the round timeout is removed outright (D8), the event log becomes an
always-on lifecycle log (D7), grouping gains a stale-index gate (D1) and a
self-modification warning (D12), each group worktree gets its own venv (D10), the
partition-only `--no-spec` harness lands as the enabler for a later grouper
session (D5), and the docs stop overstating what the system guarantees (D6, D11,
D12). The grouper redesign itself (H1–H5) is explicitly deferred.

## Problem Frame

`obs1` — the first full task-map run — completed, but only because an operator
hand-repaired it twice in 40 minutes: a healthy group killed by an arbitrary 900s
round timeout, and another killed by a transient API blip, both landing in
terminal `FAILED` requiring manual `state.json` surgery. Usage-limit
interruptions are the *common case* on real plans, not an edge. Meanwhile the run
log emitted ~4 lines per group (zero in autonomous mode), the grouper silently
dropped real symbols against a cold codegraph index, dependency-adding units were
unverifiable because the venv lives outside the worktree, and the run could not
use the very snapshot feature it was building because the orchestrator drives
itself from stale installed code. All of these are register entries D1, D7–D12
with verified citations; this document turns the settled ones into requirements.
Prior art: the register itself, ADR 0002, and the two earlier brainstorms in
`docs/brainstorms/`.

## Key Decisions

- **Envelope failures are non-terminal.** Failures typed `SessionError` (harness
  dying) get a new `INTERRUPTED` group state outside `TERMINAL_STATES`; work
  failures (coder blocked, reviewer abort, caps exhausted, operator skip) stay
  terminal `FAILED`. Classification happens at the point of failure, where the
  exception type is known — no git-state heuristics needed. *Rejected:* in-run
  auto-retry with backoff (retrying into a 5-hour usage cap needs machinery we
  don't want); an explicit `resume --retry-failed` flag (barely better than
  editing state.json).
- **Re-entry is resume-first.** A re-entered group first attempts
  `runner.resume` of the interrupted coder session — the mechanism
  `changes_required` rounds already use (`review.py:239`) — preserving its
  conversation. Fork-fresh-from-base is the fallback, not the default.
  *Rejected:* always-fork (pays cold re-orientation on every usage-limit hit).
- **The round timeout is removed, with nothing in its place.** Round duration is
  unknowable in principle; a duration cap kills healthy work (it axed `obs1`'s g1
  four units in). A wedged subprocess in an unattended run blocks until the
  operator interrupts — state stays resumable. *Rejected:* scaling the timeout
  with estimated work (still a guess, and greenfield estimates are the wrong
  ones per D4); a transcript-liveness watchdog (operator explicitly declined).
- **The event log is control-plane only, and always on.** `log_event` decouples
  from the HITL broker; lifecycle call sites are added. Coder *activity* is the
  Observatory's obligation to pull from session transcripts, never the
  orchestrator's to push. *Rejected:* orchestrator-side transcript tailing.
- **`group` syncs the index itself and fails on unknown symbols.**
  `codegraph sync` runs (blocking) before the first query, killing the
  stale-index case at the root; an existing-symbol mapping the index still
  cannot see fails the run by default (`--allow-unknown-symbols` to override).
  Prospective symbols are unaffected. *Rejected:* empty-index gate only (misses
  the mid-build case that actually bit `obs1`).
- **Each group worktree owns its environment.** `uv sync` at worktree creation;
  the worker can re-sync after editing `pyproject.toml`. This also fixes the
  worker-side half of D12: a worktree venv's editable install resolves the
  worktree's own `orchestrator/` source, so workers can exercise their own CLI
  changes. *Rejected:* orchestrator-side sync after reports (coder still can't
  test mid-round); banning dependency edits in units (pushes work to the human).
- **D12's residue is docs plus a plan-time warning.** The running orchestrator
  process can never pick up code it is currently merging — that is documented as
  a rule, and `group` warns when a plan touches `orchestrator/` source.
  *Rejected:* re-exec from the integration branch (invasive, fragile, rare plan
  shape).
- **The D5 harness lands; the H1–H5 decisions do not.** `--no-spec` and the
  deterministic fixtures record *current* behaviour as a baseline. No partitioner
  behaviour changes in this scope.

## Requirements

### Run resilience (D9, D8)

- R1. Group failures are classified at failure time: exceptions typed
  `SessionError` (or subclasses) mark the group `INTERRUPTED`; work failures
  (coder blocked/failed, reviewer abort, operator skip, caps exhausted) mark it
  `FAILED` as today. The classification is recorded on the group's state entry.
- R2. `INTERRUPTED` is a new `GroupState` outside `TERMINAL_STATES`.
  `COMPLETED`/`FAILED` semantics are unchanged.
- R3. Within a running run, an interrupted group's dependents stay `PENDING`
  (never stranded-failed). When nothing else can proceed, the run ends cleanly —
  not as a wedged-run error — with a distinct message listing interrupted groups
  and instructing `resume`.
- R4. A plain `resume` re-enters every `INTERRUPTED` group with no flags and no
  `state.json` editing, reusing the existing worktree and branch as-is
  (commits and dirty WIP intact) and the original fork-point diff base.
- R5. Re-entry attempts warm resume of the interrupted coder session first; it
  forks a fresh generation from base only when the resume attempt fails or the
  session's last known context exceeds `breaker.context_token_limit`.
- R6. Every re-entry emits a lifecycle event naming its mode and reason:
  `resumed session <sid>` or `forked generation <n> (<why>)`. There is no
  silent path.
- R7. The per-round session timeout is removed: the `session.timeout_s` config
  field (`config.py:106`), the `communicate(timeout=...)` call, and the
  `RoundTimeout` exception are deleted. The escalation-wait `timeout_s`
  (`config.py:129`) is untouched. Configs still carrying `session.timeout_s`
  produce a deprecation warning, not an error.
- R8. The run banner surfaces the effective execution config (sequential vs
  concurrency, HITL intensity) *before* launch, resolving the `obs1` operator
  trap where config silently beat expectations.
- R9. Warm resume after a hard kill is verified live once (a session killed
  mid-turn keeps completed messages but loses the partial turn); the observed
  behaviour is recorded in the register.

### Event log (D7)

- R10. `log_event` is decoupled from the escalation broker: every run mode,
  including autonomous, writes the same lifecycle events to `run.log`.
- R11. Lifecycle call sites cover: worktree creation, round start/end, reviewer
  verdicts (including each `changes_required` cycle), generation retirements and
  forks, re-entry mode (R6), merge attempt/result/conflict, and group
  completed/interrupted/failed — in addition to today's run start, coder launch,
  and escalation events.
- R12. The log format stays plain append-lines; the Observatory's `EventLog`
  renders it unchanged.

### Grouping gates (D1, D12)

- R13. `run_grouping` runs `codegraph sync` (blocking) before its first index
  query.
- R14. A task-map mapping naming an *existing* symbol the index cannot resolve
  fails the `group` run by default; `--allow-unknown-symbols` restores today's
  flag-and-drop. Prospective (`*(new)*`) symbols are unaffected either way.
- R15. `group` emits a warning when the plan's files touch `orchestrator/` own
  source: such changes take effect on the *next* run, never the one that makes
  them.

### Worktree environment (D10)

- R16. Worktree creation provisions a per-worktree venv via `uv sync`; worker
  sessions resolve that venv (no `VIRTUAL_ENV` or equivalent leakage from the
  orchestrator's own environment), so a unit that edits `pyproject.toml` can
  re-sync and import its new dependency inside the worktree.
- R17. Worker guidance (base context / coder prompt) states that dependency
  changes require `uv sync` in the worktree and that verification items
  importing new dependencies must pass there.

### Partition harness (D5)

- R18. `smart-mcps-orchestrate group <plan> --no-spec` runs every stage before
  `write_specs` and prints: the partition, the group DAG, per-node `node_work`,
  the budget cap, detected hub roles, slice atoms, and which stage last modified
  the partition. Sub-second, zero LLM calls.
- R19. `run_grouping` is refactored so the deterministic prefix is callable on
  its own; the speccer call (`pipeline.py:111`) is the seam.
- R20. Deterministic fixture plans land under `tests/fixtures/grouping/` —
  `greenfield-cross-stack`, `slice-over-budget`, `hub-in-the-middle`,
  `no-affinity-sink`, `pure-backend`, plus brownfield variants — asserted
  through the partition-only path as a recorded *baseline of current behaviour*
  (including which shapes currently cycle), not as desired behaviour.
- R21. Property tests are added where they hold today: no group's summed
  `node_work` exceeds the cap, and partitioning is byte-stable across runs.

### Partition harness — LLM-in-the-loop (D5, amendment 2026-07-22)

- R26. Opt-in LLM scenarios marked `@pytest.mark.llm`, excluded from the default
  (zero-token) run: one end-to-end `group` on a fixture plan asserting the
  `task map: parsed from plan — mapper LLM skipped` flag and non-empty specs;
  one *without* a task map, exercising the mapper fallback on a greenfield plan
  (the "drops nonexistent-file mappings" path the deterministic fixtures cannot
  cover). *(Appended post-approval when an audit showed the register's harness
  item 3 had not been carried into R18–R21; belongs with the D5 group.)*

### Documentation (D6, D11, D12, register hygiene)

- R22. `docs/orchestrator-task-map.md` is corrected to state that slice
  contraction is hard only through Louvain and may be undone by the budget
  splitter (D6). Doc-truth only; no behaviour change.
- R23. Plan-author guidance documents D11's lesson: verification items must be
  phrased behaviourally (e.g. "`GET /openapi.json` lists these paths"), never as
  framework-internal introspection.
- R24. The D12 rule — worker changes to `orchestrator/` take effect on the next
  run — is documented in the orchestrator README alongside the R15 warning.
- R25. `docs/orchestrators_improvements.md` entries D1, D7, D8, D9, D10, D12 are
  updated (status/deletion per the register's own convention) as their fixes
  land.

### Base-context hygiene (amendment 2026-07-22)

- R27. The task-map YAML block is the grouper's parser input only: every
  LLM-facing consumer of the plan text downstream of `parse_task_map` — the
  compiled base context, the speccer prompt, and run-time rewrite prompts —
  receives the plan with the marked block stripped. The plan document on disk
  is unchanged. *(Appended post-approval at the operator's request during
  planning: the map duplicates the units prose workers already receive in the
  shared context, so shipping it to every fork is pure token waste.)*

## Non-Goals

- **No grouper behaviour changes.** D2/D3/D4 and the H1–H5 decisions are
  deferred to a dedicated session that starts from the R18–R21 harness and
  baseline.
- **No timeout replacement of any kind** — no scaled timeout, no liveness
  watchdog. A wedged subprocess is the operator's call.
- **No orchestrator-side coder-activity streaming.** Mid-round liveness is the
  Observatory's obligation, pulled from session transcripts.
- **No in-run auto-retry** of envelope failures (no backoff/wait-for-limit
  machinery); recovery is via `resume`.
- **No self-drive re-exec** from the integration branch.
- **No compact-instead-of-respawn breaker redesign** — the register's open
  design discussion stays open and unprejudiced by this scope.

## Open Questions

None — every decision above was resolved with the operator on 2026-07-22.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-07-22-orchestrator-run-hardening-requirements.md`.

## Appendix — verified mechanics carried from the defect register

Reference material for planning, moved here when the covered entries were pruned
from `docs/orchestrators_improvements.md` (2026-07-22). All were verified live on
`obs1`; re-verify citations before acting.

**Resume mechanics (for R4/R5):**

- The base session (shared context) is reused on resume, not recompiled —
  `_cmd_run` reads `base_session_id` from the manifest (`cli.py:374`).
- The per-group worktree and branch are reused, not recreated — `create_worktree`
  is called with the group's existing branch and returns an existing on-branch
  worktree **as-is** (`worktrees.py:77`): no checkout, no clean, so both commits
  and dirty WIP survive. The diff base stays the original fork-point merge-base
  (`cli.py:459`).
- Within a generation, rounds resume the same coder session via
  `runner.resume(session_id=...)` (`review.py:239`) — the mechanism R5's warm
  resume generalizes. Only a new generation forks fresh from base.
- The `SessionRunner` is rebuilt from config on every `run`/`resume`
  (`cli.py:328`), so config changes take effect on resume.

**Envelope-failure signatures observed (for R1):**

- `RoundTimeout` after a healthy 15-minute round (class deleted by R7, but the
  kill path — operator interrupt, crash — still produces mid-turn deaths).
- `SessionError: claude exited 1` with *empty* stderr and a `<synthetic>` stop
  with zero token usage in the transcript — the CLI's signature for an aborted
  API call (transient usage-limit blip).
- Both landed as the same terminal `failed` string; that indistinguishability is
  what R1 fixes.

**Venv facts (for R16):**

- The venv lived at `<repo>/.venv`, outside worktrees, owned by the parent
  checkout; `uv sync` inside a worktree could not install a unit's new
  dependency, and verification items importing it were unsatisfiable from the
  worktree. Confirmed post-hoc: g1's suite collapsed at import until a manual
  `uv pip install fastapi` against the shared venv (then 328 passed / 1 failed —
  the 1 being the D11 introspection test bug).

**Stale self-drive facts (for R15/R24):**

- The installed `smart-mcps-orchestrate` console script is an editable install
  pinned to the main checkout; worker worktrees are never on the running
  interpreter's path. `obs1` could not use the per-run DAG snapshot feature it
  was itself building (`runs/obs1/groups.json` never written → `stale_dag: true`),
  and U1's `ui` subcommand was unreachable via the console script while
  `PYTHONPATH=<worktree> python -m orchestrator.cli ui --help` worked.

**Event-log baseline (for R10/R11):** six `log_event` call sites existed at
pruning time (`escalation.py` ×3, `cli.py` ×2, `review.py` ×1) — roughly four
lines per group lifetime, zero from the review loop in autonomous mode.
