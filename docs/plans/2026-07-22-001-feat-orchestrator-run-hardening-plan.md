---
title: Orchestrator Run Hardening
type: feat
date: 2026-07-22
origin: docs/brainstorms/2026-07-22-orchestrator-run-hardening-requirements.md
---

# Orchestrator Run Hardening

## Objective

Land every settled, non-grouping defect fix from the run-hardening requirements
(R1–R26) in one orchestrated scope: envelope failures become automatically
recoverable with warm session resume (R1–R6, R9), the round timeout is removed
outright (R7–R8), the event log becomes an always-on lifecycle log (R10–R12),
grouping gains a stale-index sync gate, an unknown-symbol hard failure, and a
self-modification warning (R13–R15), each group worktree gets its own venv
(R16–R17), the partition-only `--no-spec` harness lands with deterministic
fixtures and opt-in LLM scenarios (R18–R21, R26), the docs stop overstating
what the system guarantees (R22–R25), and the task-map YAML block is stripped
from every LLM-facing consumer of the plan text — it is grouper parser input,
not worker context (R27). The grouper redesign (H1–H5, D2/D3/D4) is
explicitly out of scope.

## What we already know (resolved context)

### Failure classification and state machine (u1, u2)

- `GroupState` and `TERMINAL_STATES` live at
  `orchestrator/execution/scheduler.py:46` and `:59`
  (`frozenset({COMPLETED, FAILED})`).
- **The classification point is `Scheduler._run_group`**
  (`scheduler.py:246-270`): every executor exception except `RunAbort` is
  swallowed by a broad `except Exception` at `:261` and recorded as
  `FAILED, failure=f"{type(exc).__name__}: {exc}"`. Adding an
  `except SessionError` arm above it (excluding `ReportError`) is the entire
  R1 change surface — the exception type is known right there, no git-state
  heuristics.
- `SessionError` hierarchy (`orchestrator/execution/sessions.py:55-68`):
  `PreflightError` (raised only pre-run, in `_cmd_run`, never inside a group),
  `RoundTimeout` (deleted by u3), `ReportError`. **`ReportError` is a work
  failure despite its type** (grilled decision): it is raised only after the
  session already received `DEFAULT_MAX_NUDGES = 2` warm corrective resumes
  (`sessions.py:44`, `nudge_until_report` at `:326`) — the harness was healthy,
  the agent was judged. Glossary updated accordingly (CONTEXT.md).
- `GroupFailure` (`orchestrator/execution/review.py:67`) and executor-returned
  non-terminal states stay `FAILED` (work failures: coder blocked, reviewer
  abort, operator skip, caps exhausted).
- Dependents already stay `PENDING` under a failed upstream — `remaining` only
  decrements on `COMPLETED` (`scheduler.py:232-238`). The clean-end logic is
  `_blocked_by_failure` (`scheduler.py:272-284`), whose frontier currently
  seeds from `FAILED` only; seeding it from `FAILED ∪ INTERRUPTED` makes an
  all-interrupted-blocked run end cleanly instead of raising `NoProgressError`
  (`scheduler.py:221`).
- End-of-run reporting is `_print_outcomes` (`orchestrator/cli.py:514-529`):
  prints per-group outcomes, exit 0 when all completed, else message + exit 1.
  The operator-abort path exits 2 (`cli.py:446`) — the resumable-exit
  precedent.

### Resume mechanics (u2) — verified live on obs1

- On `resume`, the base session is reused, not recompiled: `_cmd_run` reads
  `base_session_id` from the manifest (`cli.py:373-381`).
- The worktree and branch are reused as-is: `create_worktree`
  (`orchestrator/execution/worktrees.py:70-90`) returns an existing on-branch
  worktree untouched — commits and dirty WIP survive. The diff base stays the
  original fork-point merge-base (`cli.py:476`, captured in
  `_workspace_seams`).
- Scheduler resume marks every non-terminal, non-`PENDING` group `READY`
  (`scheduler.py:171-178`) and reaps orphan PIDs. `INTERRUPTED` (non-terminal)
  rides this path with zero scheduler changes — re-entry happens inside the
  executor.
- Warm resume is `SessionRunner.resume` (`sessions.py:220-224`), the exact
  mechanism `changes_required` rounds already use (`review.py:239`). Only a new
  generation forks fresh from base (`start_fork`, `review.py:178`).
- The `SessionRunner` is rebuilt from config on every `run`/`resume`
  (`cli.py:328`), so config changes take effect on resume.
- **Context tokens die with the process**: `SessionRunner._usage` is in-memory
  (`sessions.py:162`); after a restart `usage_of` returns an empty
  `SessionUsage`. Grilled decision: persist `last_context_tokens` on
  `SessionEntry` (`orchestrator/model.py:68-76`, currently
  session_id/role/generation/name/retirement_reason/transcript_path), updated
  through the existing `store.save(manifest)` path the review loop already
  calls (`review.py:554-571` `_record`, `:372` retirement).
- The coder session to warm-resume is discoverable from the manifest: the
  group's latest `SessionRole.CODER` entry with `generation ==` the persisted
  `GroupRunState.generation` and `retirement_reason is None`.
- Envelope-failure signatures observed live: `SessionError: claude exited 1`
  with empty stderr and a `<synthetic>` zero-usage stop in the transcript (the
  CLI's aborted-API-call signature); mid-turn kills keep completed messages
  but lose the partial turn (to be re-verified live by u2 per R9).
- Worker prompts are templates in `orchestrator/prompts/*.md` loaded via
  `load_template` (`orchestrator/execution/prompting.py`); a re-entry prompt is
  a new template file alongside `revision.md`/`handoff.md`.

### Round timeout (u3)

- The timeout triple: `SessionConfig.timeout_s` (`orchestrator/config.py:106`,
  default 1800.0), the `communicate(timeout=self.timeout_s)` call and
  `RoundTimeout` raise (`sessions.py:288-292`), and the `RoundTimeout` class
  (`sessions.py:63`). The escalation-wait `timeout_s` (`config.py:129`) and
  `_capture`'s own 60s preflight subprocess timeout (`sessions.py:237`) are
  untouched.
- Pydantic v2 ignores unknown keys by default, so a TOML still carrying
  `[session] timeout_s` would be *silently* dropped once the field is removed —
  the R7 deprecation warning must be detected explicitly in `load_config`
  (`config.py:145-155`) against the raw TOML dict before validation.
- The run banner currently prints *after* the base session is launched
  (`cli.py:432-436`); obs1's operator trap was `.orchestrator/config.toml`
  setting `sequential = true` + `timeout_s = 900` silently beating
  expectations. R8 moves an extended banner before any session spawn.

### Event log (u4)

- `log_event` (`orchestrator/execution/manifest.py:72`) is already
  broker-independent — O_APPEND, timestamped plain lines to
  `logs/run.log`. **The coupling is `_GroupExecution._log`**
  (`review.py:537-541`): it early-returns when `deps.broker is None`, so
  autonomous runs log nothing from the review loop. Removing that gate is R10.
- Six call sites exist today: `escalation.py` ×3, `cli.py` ×2 (HITL run-start
  at `:401`, abort at `:443`), `review.py` ×1 (via `_log`: coder launch,
  merged, completed).
- The Observatory lives on the `feat/observatory` branch, not this one — its
  `EventLog` renders `run.log` as plain lines. R12 is therefore a **format
  constraint only**: keep `{ISO-8601}  {text}` append-lines; no Observatory
  code changes in this scope.
- `tests/test_escalation.py` and `tests/test_review_loop.py` contain
  assertions built on "autonomous runs create no new artifacts"
  (`review.py:121` comment, escalation tests) — these change meaning under
  R10 and must be updated deliberately, not incidentally.

### Grouping pipeline (u5, u7, u8, u9)

- `run_grouping` (`orchestrator/grouping/pipeline.py:47`):
  `parse_task_map → [map_tasks fallback] → build_task_graph → partition → build_group_dag → write_specs`, with **the speccer call at `pipeline.py:111`
  as the R19 seam** — everything before it is deterministic and sub-second.
- `CodegraphClient` (`orchestrator/grouping/graphing.py:91`) has **no sync
  method**; its `runner` seam (injected callable) is how tests fake the CLI,
  and `run_grouping`'s first index touch is `client.files_overview()`
  (`pipeline.py:63`). R13's `codegraph sync` goes through the same runner seam
  so offline tests keep working.
- Unknown-symbol handling today: `parse_task_map` drops with an info flag
  (`task map: task t2-api mapped unknown symbol ghost_fn — dropped`,
  `tests/test_plan_reader.py:106-112`), mirroring the mapper. R14 turns the
  task-map path into a hard `GrouperError` by default;
  `--allow-unknown-symbols` restores drop-with-flag. The mapper-fallback path
  keeps its existing drop behaviour (mapper output is a guess, not a claim).
  Prospective *files* are unaffected; the map's `symbols:` field has no
  prospective notation — every listed symbol is a claim of existence.
- `DefaultPartitionStrategy.partition` stage order (`partition.py:166-186`,
  per the register): hub-role detection → slice contraction → Louvain →
  lift_independent → expansion → `split_over_budget` → `merge_small_groups`.
  "Which stage last modified the partition" (R18) requires instrumenting this
  method — comparing the partition after each stage.
- Flags land in `GroupingResult.flags` and print under `flags:` in
  `_print_report` (`cli.py:268-271`). The R15 warning and R14's override state
  belong there (plus stderr for the warning).
- Fixture repos for grouper tests are scripted per-test (`make_repo` /
  fake runner patterns in `tests/test_grouper_pipeline.py:177-189`); the suite
  is currently zero-token (StubLlm). No pytest markers are configured yet
  (`pyproject.toml` dev deps: pytest, pytest-asyncio) — R26's `llm` marker and
  a default `-m "not llm"` exclusion are new.
- **The task-map YAML currently rides into every LLM context** (u11):
  `compile_base_context` embeds the raw plan file
  (`orchestrator/grouping/base_context.py:28`, `plan_path.read_text()`), the
  speccer prompt receives the full `plan_text` (`speccer.py:74-77` via
  `pipeline.py:111`), and run-time rewrite prompts do too (`_rewrite_provider`,
  `cli.py:485-511`, fed from `plan_text` read at `cli.py:309`). The parser
  locates the block by its version-marker line, not the heading
  (docs/orchestrator-task-map.md) — the same detection a strip helper reuses.
  Side effect worth noting: `base_tokens` and thus `partition_budget_cap` are
  computed from the compiled base context (`pipeline.py:84-89`), so stripping
  slightly raises the effective per-group budget — deterministic, but fixture
  baselines (u8) must be recorded against the stripped behaviour.

### Worktree environment (u6)

- The venv lives at `<repo>/.venv`, owned by the parent checkout; `uv sync`
  inside a worktree could not install a unit's new dependency (obs1: g1's
  suite collapsed at import until a manual `uv pip install fastapi` against
  the shared venv).
- `create_worktree` (`worktrees.py:70`) is pure git and idempotent; the
  orchestrator-specific workspace assembly is the `workspace_for` seam in
  `_workspace_seams` (`cli.py:459-477`) — the right call point for
  provisioning, keeping `create_worktree` reusable.
- **Env leakage is real**: `SessionRunner._env = {**os.environ, **env}` or
  `None` (inherit) (`sessions.py:159`), passed straight to `Popen`
  (`sessions.py:283`) — a worker inherits the orchestrator's `VIRTUAL_ENV`
  and its `$VIRTUAL_ENV/bin` PATH entry, so `python`/`pytest` resolve the
  parent venv from inside the worktree.
- Worker guidance surfaces: `orchestrator/prompts/coder.md` and `handoff.md`
  (both open a generation), rendered by `render_coder_prompt` /
  `render_handoff_prompt` (`prompting.py:45`, `:86`).

### Docs surfaces (u10)

- `docs/orchestrator-task-map.md` — states "Slice-mates are contracted into
  one node before Louvain — a hard must-link"; the register (D2/D6) proved
  *current enforcement* holds only through Louvain and `split_over_budget` may
  undo it (dissolved 3/3 slices in both live runs). Meanwhile CONTEXT.md
  (2026-07-22) declares Slice a hard output invariant — the *target* semantics
  the parallel grouping plan implements. R22 therefore documents the gap, not
  a softer contract: must-link is the declared semantics; today's
  implementation enforces it only through Louvain.
- `skills/orchestrator-plan/SKILL.md` — the single copy (`.claude/skills` is a
  symlink to `../skills`); R23's behavioural-verification guidance goes here
  (grilled decision), with a one-line cross-reference from the task-map doc.
- `orchestrator/README.md` exists — R24's home for the D12 rule.
- `docs/orchestrators_improvements.md` — the register: D2/D3/D4 remain open
  (grouper study); D1, D5–D12 are absorbed into the origin brainstorm with an
  absorption note at the top and one-line records in the obs1 section. R25
  updates these per the register's own convention as fixes land. **u2 also
  appends its R9 observation to this file** — u2 writes only into the obs1
  live-run section, u10 only the absorbed/status blocks, to keep the
  concurrent edits mergeable.

### Meta (affects how this plan runs)

- **This plan triggers its own R15 scenario**: it edits `orchestrator/` source,
  so the run executing it is driven by pre-hardening code (D12) — the old
  round-timeout behaviour, terminal envelope failures, and the sparse event
  log all still apply to *this* run. Interruptions during it are recovered the
  obs1 way (manual state surgery) one last time.
- **The task map deliberately carries no `symbols`.** The first dry-run
  failed with `dependency cycle across groups [0, 2, 3, 4, 5]`: listed symbols
  generate directed call/impact edges (`graphing.py:254-283`), and on this
  self-hosted brownfield plan — where seven units legitimately co-edit
  `cli.py` and the execution modules call each other — those edges form a
  near-complete bidirectional web no partition can quotient acyclically
  (D3, live-confirmed). `symbols: []` removes exactly that signal class;
  shared-file affinity (rich here), declared `depends_on`
  (`graphing.py:242-245`, independent of symbols), and route tags remain.
  All symbol names cited in unit prose were verified against a freshly synced
  index at plan time (the D1 lesson, applied manually since R13 doesn't exist
  yet).
- **A grouping plan is being written in parallel** (separate session; its
  glossary terms — hard Slice invariant, Grouping Trace, Size Hint — already
  landed in CONTEXT.md). That plan builds on this one's u7/u8 harness and
  baseline, so this plan must land first; nothing here may pre-empt its
  surface (`split_over_budget`, `merge_small_groups` behaviour, task-map
  schema) beyond the R18–R21 instrumentation.

## Decisions

- **`ReportError` classifies as work failure, not envelope failure.** R1's
  letter says "SessionError or subclasses", but a nudge-exhausted coder
  already received 2 warm corrective resumes — the harness was healthy and
  the work was judged. Classification = `SessionError`-or-subclass **except**
  `ReportError`. Glossary updated. *Rejected:* following R1's letter (creates
  an operator-driven infinite retry loop for a persistently report-broken
  session); re-typing `ReportError` outside the hierarchy (touches every
  except-clause for a taxonomy nicety).
- **`last_context_tokens` persists on `SessionEntry`.** The R5 pre-check needs
  the interrupted session's context size after a process restart; the manifest
  is the file re-entry already reads the session id from, and the review loop
  already saves it every round. *Rejected:* parsing the transcript tail
  (couples the orchestrator to the data-plane format the Observatory owns);
  resuming blind (one potentially oversized round before the breaker can see
  it, and R5's wording implies a pre-check).
- **R9's live experiment runs inside the u2 worker.** The worker has the real
  CLI and credentials: start a cheap `claude -p` session, SIGKILL it mid-turn,
  `--resume` it, record what survived into the register's obs1 section.
  *Rejected:* shipping an operator-run script (leaves R9 pending on a human);
  waiting for the next natural incident (indefinite).
- **R23 guidance lives in `skills/orchestrator-plan/SKILL.md`.** That skill is
  where verification items are authored — the lesson sits where the mistake
  gets made; the task-map doc gets a one-line cross-reference. *Rejected:*
  task-map doc as primary home (verification items aren't part of the map);
  duplicating in full (drift).
- **Warm-resume failure falls through to fork; fork failure re-interrupts.**
  A `SessionError` during the warm attempt (e.g. session store lost the
  conversation) triggers the fork-fresh fallback with a logged reason; a
  `SessionError` from the fork itself propagates to the scheduler and lands
  as `INTERRUPTED` again — correct, since the envelope is still failing (e.g.
  the usage limit is still active). No in-run retry loop (origin non-goal).
- **Interrupted clean-end exits 2.** Mirrors the operator-abort exit
  (`cli.py:446`): 2 = stopped-but-resumable, 1 = needs-inspection failure,
  0 = complete. The distinct message lists interrupted groups and the exact
  `resume` command.
- **Venv provisioning is a `worktrees.py` function called from the
  `workspace_for` seam, skipped for non-uv repos.** `provision_env(worktree)`
  runs `uv sync` iff `pyproject.toml` or `uv.lock` exists at the worktree
  root; failure is non-fatal (lifecycle event + stderr) since the worker can
  re-sync per its guidance. `create_worktree` stays pure git. Env hygiene
  lives in `SessionRunner`: drop `VIRTUAL_ENV` and its `bin` PATH entry from
  the worker env. *Rejected:* provisioning inside `create_worktree` (couples
  git plumbing to Python tooling); hard-failing the group on sync errors
  (turns a fixable env hiccup into a dead group).
- **The `session.timeout_s` deprecation warning is raw-TOML detection.**
  Pydantic v2 silently ignores unknown keys, so `load_config` inspects the
  parsed TOML dict for `session.timeout_s` before validation and warns to
  stderr. *Rejected:* `extra="forbid"` on `SessionConfig` (R7 explicitly wants
  a warning, not an error).
- **Stage attribution instruments `DefaultPartitionStrategy` directly.** R18's
  "which stage last modified the partition" is recorded by comparing the
  partition after each internal stage (contraction/Louvain/lift/split/merge)
  — no parallel re-computation. *Rejected:* re-running stages outside the
  strategy to diff them (duplicates the pipeline it's meant to observe).
- **R11 call sites land with their owning units.** Group
  interrupted/failed events are emitted where u1 writes the classification;
  the re-entry event (R6) where u2 writes re-entry; u4 owns the rest
  (worktree creation, round start/end, verdicts, retirements/forks, merge
  attempt/result/conflict, completed) plus the R10 decoupling. Splitting a
  log line from the code that creates the moment it logs would force three
  units through one function.
- **Map `symbols` are empty across the plan.** The dry-run proved that on a
  brownfield plan whose units co-edit hub files, symbol-derived call/impact
  edges make the group DAG cycle unconditionally (D3) — the cycle error named
  bidirectional edges between nearly every unit pair. Ordering stays fully
  expressed via `depends_on`; clustering stays driven by shared files and
  route tags; the unit prose and verification bullets keep naming the anchor
  symbols for workers. *Rejected:* pruning symbols case-by-case (iterating a
  plan to satisfy the partitioner is the register's named anti-pattern, and
  any symbol called from a co-owned file re-creates the web); waiting for H5
  cycle repair (that lands in the parallel grouping plan, which needs this
  plan's harness first).
- **The task map is stripped downstream of the parser, never from the plan
  file.** A `strip_task_map` helper beside `parse_task_map` (same
  version-marker detection) removes the fenced block — plus a directly
  preceding `## Task Map` heading — from the text handed to the base-context
  compiler, the speccer, and the rewrite provider. The plan document on disk
  keeps the map: it *is* the grouper's input and the format contract's home.
  *Rejected:* stripping only the base context (speccer and rewrite prompts
  pay the same duplication on every call); deleting the block from the plan
  after grouping (breaks re-grouping and the 1:1 prose↔map contract).
- **The new glossary slice semantics do not expand this scope.** CONTEXT.md
  now declares Slice a hard invariant (plus Grouping Trace and Size Hint) —
  decisions of the grouping plan being written in parallel. This plan keeps
  the approved R1–R26 scope; R22 is rephrased to document the
  declared-vs-enforced gap rather than a softer contract, and u8's fixtures
  record the *current* (soft) behaviour as the baseline that grouping plan
  starts from. *Rejected:* folding hard-slice enforcement, the trace sidecar,
  or size hints into this plan (contradicts the origin's non-goals and
  pre-empts the parallel plan's surface).

No ADRs promoted: every decision above is either easily reversible (timeout
removal, exit codes, provisioning seam) or already carries its permanent
record in the origin brainstorm and the glossary (INTERRUPTED semantics,
resume-first re-entry).

## Units

### U1. u1-interrupted-state — INTERRUPTED joins the state machine; classification at the point of failure

- **Goal**: Envelope failures land as non-terminal `INTERRUPTED` (R1, R2);
  dependents wait and an all-blocked run ends cleanly with a distinct,
  resume-instructing message (R3).
- **Files**: `orchestrator/execution/scheduler.py`, `orchestrator/cli.py`,
  `tests/test_scheduler.py`, `tests/test_cli.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: interrupt-resume
- **Implements / Consumes**: implements `state:interrupted`
- **Verification**:
  - `GroupState.INTERRUPTED` exists and is not in `TERMINAL_STATES`;
    `COMPLETED`/`FAILED` membership unchanged.
  - An executor raising `SessionError` marks the group `interrupted` in
    `state.json` with the failure text recorded; raising `ReportError` or
    `GroupFailure` marks it `failed` (test via scripted executors in
    `tests/test_scheduler.py`).
  - With g2 depending on interrupted g1, g2 ends the run in state `pending`
    (never `failed`), and `scheduler.run()` returns instead of raising
    `NoProgressError`.
  - A run ending with interrupted groups prints a message naming each
    interrupted group and the literal command
    `smart-mcps-orchestrate resume <run_id>`, and exits 2; a run ending with
    only work-failures keeps today's message and exit 1.
  - `log_event` writes `group <gid>: interrupted (<reason>)` /
    `group <gid>: failed (<reason>)` lines at classification time.
  - Full suite passes.

### U2. u2-reentry — resume-first re-entry of interrupted groups

- **Goal**: A plain `resume` re-enters every `INTERRUPTED` group in its
  existing worktree, warm-resuming the interrupted coder session first and
  forking from base only on failure or context overflow, with every re-entry
  logged (R4, R5, R6); warm-resume-after-kill is verified live once (R9).
- **Files**: `orchestrator/execution/review.py`, `orchestrator/model.py`,
  `orchestrator/prompts/reentry.md` *(new)*, `tests/test_review_loop.py`,
  `tests/test_model.py`, `docs/orchestrators_improvements.md`
- **Symbols**: —
- **Depends-on**: u1-interrupted-state, u4-lifecycle-log
- **Slice**: interrupt-resume
- **Implements / Consumes**: consumes `state:interrupted`, `log:lifecycle`
- **Verification**:
  - `SessionEntry` gains `last_context_tokens: int = 0`; the review loop
    updates the active coder's entry after every round (round-trip asserted in
    `tests/test_model.py`, update-per-round in `tests/test_review_loop.py`).
  - On re-entry (manifest holds a live coder entry for the persisted
    generation), the executor issues `runner.resume` against that session id
    with the reentry prompt — asserted via StubRunner: no `start_fork` call
    for the resumed generation.
  - When the persisted `last_context_tokens` exceeds
    `breaker.context_token_limit`, or the warm resume raises `SessionError`,
    the executor forks a fresh generation from base (existing handoff-free
    coder prompt) and the group still completes.
  - Every re-entry writes exactly one lifecycle line:
    `group <gid> re-entry: resumed session <sid>` or
    `group <gid> re-entry: forked generation <n> (<reason>)` — asserted in
    autonomous mode (no broker).
  - A fork attempt that itself raises `SessionError` propagates (group lands
    `interrupted` again — asserted with u1's scheduler classification).
  - R9 live check (worker-executed, real CLI): start a `claude -p` session
    with a fixed `--session-id`, SIGKILL the process mid-turn, `--resume` the
    same id with "summarize our conversation so far"; the resumed reply
    references content from completed turns and not the killed partial turn.
    The observed behaviour is appended to the obs1 section of
    `docs/orchestrators_improvements.md` (that section only — the
    absorbed/status blocks belong to u10).
  - Full suite passes.

### U3. u3-timeout-removal — delete the round timeout; surface the effective config before launch

- **Goal**: No per-round subprocess timeout exists anywhere (R7); the operator
  sees the effective execution config before any session spawns (R8).
- **Files**: `orchestrator/execution/sessions.py`, `orchestrator/config.py`,
  `orchestrator/cli.py`, `tests/test_sessions.py`, `tests/test_cli.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `RoundTimeout`, `SessionConfig.timeout_s`, `SessionRunner.timeout_s`, and
    the `communicate(timeout=...)` argument are gone;
    `grep -rn "RoundTimeout\|timeout_s" orchestrator/` shows only
    `escalation` timeout uses and the preflight `_capture` timeout.
  - A round outlasting the old default completes normally (fake_claude with a
    scripted delay longer than any prior `timeout_s` test value).
  - `load_config` on a TOML containing `[session] timeout_s = 900.0` returns a
    valid config and prints a deprecation warning naming the key to stderr;
    the same TOML without the key warns nothing.
  - The escalation-wait `timeout_s` (`EscalationConfig`) still round-trips
    through config and CLI flags unchanged.
  - `run` prints, before the base session is created, one banner naming: run
    id, group count, `sequential` or `concurrency N`, HITL enabled/disabled
    with intensity and source when enabled, and permission mode — asserted
    by output-order in `tests/test_cli.py` (banner line precedes the base
    session's spawn in the fake CLI's call log).
  - Full suite passes.

### U4. u4-lifecycle-log — the event log is control-plane only, and always on

- **Goal**: Every run mode, including autonomous, writes the same lifecycle
  events to `run.log` (R10), covering the full R11 call-site list, in the
  unchanged plain append-line format (R12).
- **Files**: `orchestrator/execution/review.py`, `orchestrator/cli.py`,
  `tests/test_review_loop.py`, `tests/test_escalation.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `log:lifecycle`
- **Verification**:
  - `_GroupExecution._log` no longer checks `deps.broker`; a fully autonomous
    review-loop run (broker=None, policy=None) produces `run.log` lines.
  - One autonomous end-to-end review-loop scenario asserts lines for: worktree
    creation, round start and end, each reviewer verdict including every
    `changes_required` cycle, a generation retirement and the follow-up fork,
    merge attempt and result, and group completed; a conflict scenario asserts
    the merge-conflict line; failure lines are u1's (asserted there).
  - Existing tests asserting "autonomous runs create no artifacts" are
    updated to assert "no *escalation* artifacts" — escalation request/response
    behaviour itself is unchanged.
  - Every emitted line matches `^\d{4}-\d{2}-\d{2}T[^ ]+  ` (the existing
    `log_event` format) — no structured/JSON lines (R12).
  - Full suite passes.

### U5. u5-grouping-gates — sync before grouping, fail on unknown symbols, warn on self-modification

- **Goal**: `group` can no longer run against a stale index (R13), silently
  drop a claimed-existing symbol (R14), or silently plan a self-modification
  the running orchestrator cannot use (R15).
- **Files**: `orchestrator/grouping/pipeline.py`,
  `orchestrator/grouping/plan_reader.py`, `orchestrator/grouping/graphing.py`,
  `orchestrator/cli.py`, `tests/test_plan_reader.py`,
  `tests/test_grouper_pipeline.py`
- **Symbols**: —
- **Depends-on**: u7-nospec-harness
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `CodegraphClient.sync()` exists (via the injectable runner seam) and
    `run_grouping` invokes it, blocking, before `files_overview` — asserted
    with a recording fake runner (sync call ordered first).
  - A task map naming an unknown symbol raises `GrouperError` naming the task
    and symbol; with `--allow-unknown-symbols` the same plan groups with
    today's `— dropped` flag. Prospective files behave identically in both
    modes.
  - The mapper-fallback path (no task map) keeps drop-with-flag regardless of
    the flag.
  - A plan whose mappings touch any path under `orchestrator/` produces a
    flag and a stderr warning stating the changes take effect on the next
    run; a plan touching no `orchestrator/` path produces neither.
  - Full suite passes.

### U6. u6-worktree-venv — each group worktree owns its environment

- **Goal**: Worktree creation provisions a per-worktree venv and workers
  resolve it — no `VIRTUAL_ENV` leakage — so dependency-editing units can
  `uv sync` and import their new dependency in place (R16); worker guidance
  says so (R17).
- **Files**: `orchestrator/execution/worktrees.py`,
  `orchestrator/execution/sessions.py`, `orchestrator/cli.py`,
  `orchestrator/prompts/coder.md`, `orchestrator/prompts/handoff.md`,
  `tests/test_sessions.py`, `tests/test_cli.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `provision_env(worktree)` runs `uv sync` in the worktree iff
    `pyproject.toml` or `uv.lock` exists there; a repo without either is
    skipped silently; a failing sync logs an event and does not raise
    (subprocess seam faked in tests).
  - `workspace_for` calls `provision_env` after `create_worktree`.
  - The env passed to worker subprocesses contains no `VIRTUAL_ENV`, and
    `PATH` contains no entry under the orchestrator's own `VIRTUAL_ENV` —
    asserted by inspecting the fake CLI's recorded environment when the
    orchestrator itself runs with `VIRTUAL_ENV` set.
  - In a real uv worktree (this repo, git worktree in tmp), after
    `uv sync` a `uv run python -c "import orchestrator"` resolves to the
    worktree's own source tree (`orchestrator.__file__` under the worktree
    path) — the D12 worker-side fix.
  - `coder.md` and `handoff.md` rendered prompts state that dependency
    changes require `uv sync` inside the worktree and that verification items
    importing new dependencies must pass there.
  - Full suite passes.

### U7. u7-nospec-harness — the partition-only path and `--no-spec`

- **Goal**: `smart-mcps-orchestrate group <plan> --no-spec` answers "how would
  this plan group?" in under a second with zero LLM calls (R18), on a
  deterministic prefix of `run_grouping` that is callable on its own (R19).
- **Files**: `orchestrator/grouping/pipeline.py`,
  `orchestrator/grouping/partition.py`, `orchestrator/cli.py`,
  `tests/test_grouper_pipeline.py`, `tests/test_partition.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: partition-harness
- **Implements / Consumes**: implements `cli:no-spec`
- **Verification**:
  - A partition-only function exists in `pipeline.py` returning the mappings,
    task graph, partition, group DAG, per-node `node_work`, budget cap, hub
    roles, slice atoms, and last-modifying stage; `run_grouping` is
    re-expressed as that function plus the speccer + assembly (behaviour of
    the full path unchanged — existing pipeline tests pass untouched except
    for import churn).
  - `DefaultPartitionStrategy` records which internal stage (contraction /
    louvain / lift / split / merge) last changed the partition; unit-asserted
    on shapes that exercise `split_over_budget` and `merge_small_groups`.
  - `group <plan> --no-spec` prints all R18 items and never invokes the LLM
    runner (asserted with a runner stub that raises on call).
  - `--no-spec` on a task-map plan completes in under one second (asserted
    with a generous wall-clock bound in the test).
  - Full suite passes.

### U8. u8-grouping-fixtures — deterministic fixture plans and property baseline

- **Goal**: The register's five shapes plus a brownfield variant exist as
  fixture plans asserted through the partition-only path as a recorded
  baseline of *current* behaviour — including which shapes currently cycle
  (R20) — plus the property tests that hold today (R21).
- **Files**: `tests/fixtures/grouping/greenfield-cross-stack.md` *(new)*,
  `tests/fixtures/grouping/slice-over-budget.md` *(new)*,
  `tests/fixtures/grouping/hub-in-the-middle.md` *(new)*,
  `tests/fixtures/grouping/no-affinity-sink.md` *(new)*,
  `tests/fixtures/grouping/pure-backend.md` *(new)*,
  `tests/fixtures/grouping/brownfield-cross-stack.md` *(new)*,
  `tests/test_grouping_fixtures.py` *(new)*
- **Symbols**: —
- **Depends-on**: u7-nospec-harness
- **Slice**: partition-harness
- **Implements / Consumes**: consumes `cli:no-spec`
- **Verification**:
  - Each fixture is a self-contained task-map plan paired with a scripted
    fixture repo (brownfield variants create real files; greenfield ones
    don't), runnable through the partition-only path with a stub codegraph
    runner — zero LLM calls, zero real codegraph.
  - Each fixture's test records the observed outcome as the baseline:
    partition membership for non-cycling shapes, `GroupCycleError` for
    cycling ones — with a comment stating these are *current* behaviour, not
    desired (the grouper study will move them).
  - Property tests: for every non-cycling fixture, no group's summed
    `node_work` exceeds the budget cap; running the partition twice yields
    byte-identical serialized partitions.
  - The suite stays zero-token: no fixture test touches an LLM runner.
  - Full suite passes.

### U9. u9-llm-scenarios — opt-in LLM-in-the-loop tests

- **Goal**: Two `@pytest.mark.llm` scenarios exercise the paths the
  deterministic fixtures cannot, excluded from the default zero-token run
  (R26).
- **Files**: `tests/test_grouping_llm.py` *(new)*, `pyproject.toml`
- **Symbols**: —
- **Depends-on**: u8-grouping-fixtures
- **Slice**: partition-harness
- **Implements / Consumes**: —
- **Verification**:
  - The `llm` marker is registered in `pyproject.toml` and the default
    `pytest` invocation excludes it (`-m "not llm"` via addopts); a plain
    `uv run pytest` collects zero `llm` tests, `uv run pytest -m llm`
    collects exactly two.
  - Scenario 1: end-to-end `group` on a task-map fixture plan asserts the
    `task map: parsed from plan — mapper LLM skipped` flag and non-empty
    specs for every group.
  - Scenario 2: a greenfield plan *without* a task map runs the mapper
    fallback and the result records the mapper's drops-nonexistent-file
    behaviour (flags present, prospective files absent from mappings) — the
    path deterministic fixtures cannot cover.
  - The default (non-llm) suite still passes and remains zero-token.

### U10. u10-docs-register — the docs stop overstating; the register reflects the landings

- **Goal**: The task-map doc states the declared-vs-enforced slice gap (R22,
  reconciled with CONTEXT.md's hard-invariant target), plan authors are told
  to phrase verification behaviourally (R23), the D12 rule is in the
  orchestrator README (R24), and the register's D1, D7–D12 entries are
  updated per its convention (R25).
- **Files**: `docs/orchestrator-task-map.md`,
  `skills/orchestrator-plan/SKILL.md`, `orchestrator/README.md`,
  `docs/orchestrators_improvements.md`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `docs/orchestrator-task-map.md`'s `slice` row states: must-link is the
    declared semantics (per CONTEXT.md's Slice entry), but current enforcement
    holds only through Louvain and may be undone by the budget splitter —
    citing the register finding and naming the gap as the grouping plan's to
    close. No other contract semantics change.
  - `skills/orchestrator-plan/SKILL.md` contains the D11 lesson: verification
    items phrased behaviourally (with the `GET /openapi.json` example),
    never framework-internal introspection; the task-map doc cross-references
    it in one line.
  - `orchestrator/README.md` documents: worker changes to `orchestrator/`
    take effect on the next run, never the one that makes them, alongside a
    mention of the `group`-time warning.
  - `docs/orchestrators_improvements.md`: D1, D7, D8, D9, D10, D12 entries
    are updated/pruned per the register's convention (absorbed-note updated
    to "landed" with the plan path); D2/D3/D4 remain untouched and open.
    Edits stay out of the obs1 live-run section (u2's append target).
  - Prose-only unit: `uv run pytest` collects and passes unchanged.

### U11. u11-taskmap-strip — the task map never reaches an LLM context

- **Goal**: The task-map YAML block is grouper parser input only: the compiled
  base context, the speccer prompt, and run-time rewrite prompts all receive
  the plan text with the marked block stripped; the plan file on disk is
  untouched (R27).
- **Files**: `orchestrator/grouping/plan_reader.py`,
  `orchestrator/grouping/base_context.py`, `orchestrator/grouping/pipeline.py`,
  `orchestrator/cli.py`, `tests/test_plan_reader.py`,
  `tests/test_grouper_pipeline.py`
- **Symbols**: —
- **Depends-on**: u7-nospec-harness
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - A `strip_task_map(text)` helper in `plan_reader.py` removes exactly the
    version-marker-located fenced block plus a directly preceding `## Task Map` heading, leaving all other content byte-identical; text without a
    marked block passes through unchanged (asserted in
    `tests/test_plan_reader.py`).
  - The compiled base context for a task-map plan contains the plan's unit
    prose but neither the `# orchestrator-task-map v1` marker nor a dangling
    `## Task Map` heading; base-context compilation stays byte-stable.
  - The speccer prompt (captured via the stub LLM runner) and a rewrite
    prompt (via the rewrite-provider seam) contain no version marker.
  - The plan file on disk is byte-identical before and after `group`.
  - `partition_budget_cap` is computed from the stripped base context —
    existing pipeline tests updated where the head size changes; u8's fixture
    baselines record post-strip numbers.
  - Full suite passes.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-interrupted-state
    description: Add non-terminal INTERRUPTED group state with envelope/work classification at the scheduler and a clean resume-instructing run end
    slice: interrupt-resume
    files:
      - orchestrator/execution/scheduler.py
      - orchestrator/cli.py
      - tests/test_scheduler.py
      - tests/test_cli.py
    symbols: []
    depends_on: []
    implements: ["state:interrupted"]
    consumes: []
  - task_id: u2-reentry
    description: Resume-first re-entry of interrupted groups with warm session resume, persisted context tokens, logged re-entry mode, and the live R9 kill-resume verification
    slice: interrupt-resume
    files:
      - orchestrator/execution/review.py
      - orchestrator/model.py
      - orchestrator/prompts/reentry.md
      - tests/test_review_loop.py
      - tests/test_model.py
      - docs/orchestrators_improvements.md
    symbols: []
    depends_on: [u1-interrupted-state, u4-lifecycle-log]
    implements: []
    consumes: ["state:interrupted", "log:lifecycle"]
  - task_id: u3-timeout-removal
    description: Delete the per-round session timeout and RoundTimeout entirely, warn on deprecated config, and print the effective execution config before launch
    slice: null
    files:
      - orchestrator/execution/sessions.py
      - orchestrator/config.py
      - orchestrator/cli.py
      - tests/test_sessions.py
      - tests/test_cli.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u4-lifecycle-log
    description: Decouple the event log from the HITL broker and add lifecycle call sites so autonomous runs write the same run.log
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/cli.py
      - tests/test_review_loop.py
      - tests/test_escalation.py
    symbols: []
    depends_on: []
    implements: ["log:lifecycle"]
    consumes: []
  - task_id: u5-grouping-gates
    description: Blocking codegraph sync before grouping, hard failure on unknown task-map symbols with an override flag, and a warning when a plan touches orchestrator source
    slice: null
    files:
      - orchestrator/grouping/pipeline.py
      - orchestrator/grouping/plan_reader.py
      - orchestrator/grouping/graphing.py
      - orchestrator/cli.py
      - tests/test_plan_reader.py
      - tests/test_grouper_pipeline.py
    symbols: []
    depends_on: [u7-nospec-harness]
    implements: []
    consumes: []
  - task_id: u6-worktree-venv
    description: Per-worktree venv provisioning via uv sync at worktree creation, VIRTUAL_ENV scrubbing for worker sessions, and dependency-workflow worker guidance
    slice: null
    files:
      - orchestrator/execution/worktrees.py
      - orchestrator/execution/sessions.py
      - orchestrator/cli.py
      - orchestrator/prompts/coder.md
      - orchestrator/prompts/handoff.md
      - tests/test_sessions.py
      - tests/test_cli.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u7-nospec-harness
    description: Refactor run_grouping so the deterministic prefix is callable alone and add the sub-second zero-LLM group --no-spec report with partition stage attribution
    slice: partition-harness
    files:
      - orchestrator/grouping/pipeline.py
      - orchestrator/grouping/partition.py
      - orchestrator/cli.py
      - tests/test_grouper_pipeline.py
      - tests/test_partition.py
    symbols: []
    depends_on: []
    implements: ["cli:no-spec"]
    consumes: []
  - task_id: u8-grouping-fixtures
    description: Deterministic fixture plans for the register shapes asserted through the partition-only path as a current-behaviour baseline plus budget and byte-stability property tests
    slice: partition-harness
    files:
      - tests/fixtures/grouping/greenfield-cross-stack.md
      - tests/fixtures/grouping/slice-over-budget.md
      - tests/fixtures/grouping/hub-in-the-middle.md
      - tests/fixtures/grouping/no-affinity-sink.md
      - tests/fixtures/grouping/pure-backend.md
      - tests/fixtures/grouping/brownfield-cross-stack.md
      - tests/test_grouping_fixtures.py
    symbols: []
    depends_on: [u7-nospec-harness]
    implements: []
    consumes: ["cli:no-spec"]
  - task_id: u9-llm-scenarios
    description: Two opt-in pytest.mark.llm scenarios covering the task-map fast path end to end and the mapper fallback, excluded from the default zero-token run
    slice: partition-harness
    files:
      - tests/test_grouping_llm.py
      - pyproject.toml
    symbols: []
    depends_on: [u8-grouping-fixtures]
    implements: []
    consumes: []
  - task_id: u10-docs-register
    description: Correct the slice must-link claim, add behavioural-verification plan guidance, document the D12 next-run rule, and update the register entries as fixes land
    slice: null
    files:
      - docs/orchestrator-task-map.md
      - skills/orchestrator-plan/SKILL.md
      - orchestrator/README.md
      - docs/orchestrators_improvements.md
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u11-taskmap-strip
    description: Strip the task-map YAML block from every LLM-facing consumer of the plan text so workers, speccer, and rewrite prompts never pay for the grouper's parser input
    slice: null
    files:
      - orchestrator/grouping/plan_reader.py
      - orchestrator/grouping/base_context.py
      - orchestrator/grouping/pipeline.py
      - orchestrator/cli.py
      - tests/test_plan_reader.py
      - tests/test_grouper_pipeline.py
    symbols: []
    depends_on: [u7-nospec-harness]
    implements: []
    consumes: []
```
