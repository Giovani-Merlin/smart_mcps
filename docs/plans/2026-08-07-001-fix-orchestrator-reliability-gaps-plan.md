---
title: Close the remaining orchestrator reliability gaps
type: fix
date: 2026-08-07
origin: direct
---

# Close the remaining orchestrator reliability gaps

## Objective

Five items were re-triaged against **current** code (not the 8-9-day-stale memory
notes) on `feat/multiagent-orchestrator`. Four are real and unfixed; a fifth
(HITL permission routing) is real but architecturally open and is scoped down to
a design-only unit here so it doesn't block the other four. When this plan's
units land: a merge conflict gets one in-place resolve attempt on the same warm
coder session before falling back to today's rewrite path; `resume` without
`--intensity`/`--hitl`/etc. restores the original run's escalation tier instead
of silently resetting to `on_stuck`/HITL-on; a usage-limit failure's error
message carries the CLI's actual `result` text instead of being empty; every
coder commit subject has no leading whitespace; and the permission-routing gap
has a written design proposal ready for a future implementation plan.

## What we already know (resolved context)

**U1 — merge-conflict resolve-in-place**

- `IntegrationMerger.merge_group()` (`orchestrator/execution/merge.py:73-104`)
  attempts `git merge --no-ff`; on conflict it aborts the merge (`git merge --abort`) and raises `MergeConflict(message, affected_groups=[group.id, *self._groups_owning(conflicted)])`. The integration worktree is left clean
  either way — a resolve attempt cannot corrupt it, it just retries the same
  merge later.
- `_GroupExecution._merge()` (`orchestrator/execution/review.py:472-495`) is the
  only caller: on `MergeConflict` it marks a `Surprise(kind="merge_conflict", ...)`, spreads it to affected groups, escalates
  (`EscalationKind.MERGE_CONFLICT`), and unconditionally calls
  `self._rewrite(...)` — a full spec-regeneration via `deps.rewrite_spec`
  (an LLM call) plus a **fresh** coder session next generation. There is no
  attempt to have the *existing* warm coder session (still holding full context
  of what it just built) resolve the conflict in place.
- `self.coder_sid` (set in `_run_generation`, `review.py:227`) is the live
  session id for the group's current coder. `self.deps.runner.resume(session_id= self.coder_sid, prompt=..., cwd=self.workspace)` is the existing pattern for
  warm-resuming a session (used for `revision`/`needs_input`/`extra_pass`
  elsewhere in this same class) — it returns a `RoundResult` that then needs to
  go through `nudge_until_report(...)` like every other round to get back to a
  `CoderReport`.
- `ExecutionConfig` (`orchestrator/config.py:117-128`) has `max_rewrites: int = 2` as the existing sibling cap. Add `max_conflict_resolve_attempts: int = 1`
  next to it (confirmed default: 1 — this repo runs groups serially by default,
  `concurrency: int = 1`, `orchestrator/config.py:123`, so cross-group merge
  conflicts should be rare; one resolve attempt before falling to the proven
  rewrite path is the right cost/benefit).
- Prompt templates live as `.md` files under `orchestrator/prompts/`, rendered
  via `Template(...).substitute(...)` in `orchestrator/execution/prompting.py`
  (see `render_reentry_prompt` at `prompting.py:72-75` and
  `orchestrator/prompts/reentry.md` for the exact pattern to follow — a short,
  role-preserving re-orientation prompt, not a fresh identity block). A new
  `orchestrator/prompts/conflict_resolve.md` + `render_conflict_resolve_prompt()`
  follows this pattern, substituting in the conflicted file list and the
  integration branch name so the coder knows to `git fetch`/rebase or hand-merge
  inside its own worktree.
- Existing coverage to extend: `tests/test_review_loop.py:: test_merge_conflict_routes_to_rewriting_and_fans_out_a_surprise` (line 460,
  currently asserts a conflict routes straight to `REWRITING`) and
  `tests/test_review_loop.py::test_rewrite_cap_fails_the_group`. Both assume
  today's conflict → rewrite behavior and need updating for the new
  resolve-then-rewrite-on-exhaustion order; `tests/test_merge.py` covers
  `merge_group`'s conflict path directly and is unaffected by this unit (the
  fix is entirely on the `_merge`/`_GroupExecution` side, not inside
  `IntegrationMerger`).

**U2 — `resume` drops escalation config**

- `EscalationConfig` (`orchestrator/config.py:162-184`) defaults to `enabled= True, intensity="on_stuck"` — i.e. HITL-on-and-blocking is the *default*, not
  an opt-in.
- `RunManifest` (`orchestrator/model.py:90-98`) is the run-identity record —
  `run_id`, `plan_path`, `created_at`, `base_session_id`, `grouping` — written
  once at `run` time (`orchestrator/cli.py:782-788`) via `ManifestStore.save`
  and reused verbatim on `resume` (`cli.py:763-772`, loaded via `store.load()`).
  It is the correct home for a persisted `escalation: EscalationConfig | None = None` field (confirmed by user preference): it already carries other
  once-set, run-identity facts, unlike `RunState`/`state.json`
  (`orchestrator/execution/scheduler.py:147-151`), which is rewritten after
  every group transition and has no natural "run-wide config" slot.
- **Ordering hazard already present in `_cmd_run`** (`orchestrator/cli.py:590- 826`) that this unit must resolve: `config = _load_config(args, repo_root)`
  runs at line 592 — before `run_id`/`paths` even exist (line 596-597) — and the
  `EscalationBroker`/`EscalationPolicy` are constructed from that `config` at
  lines 721-735, which is still *before* `manifest = store.load()` at line 768.
  So today, nothing in `_cmd_run` has read the manifest by the time escalation
  config is finalized. Fixing this requires: for `resume`, peek the persisted
  manifest's `escalation` field *before* calling `_load_config`/building the
  broker — i.e. construct `ManifestStore(paths)` and call `.load()` once,
  early, right after `paths = RunPaths(repo_root, run_id)` (line 597) but
  before `config = _load_config(...)` — then reuse that same loaded `manifest`
  object at line 768 instead of loading it a second time. `ManifestStore.load()`
  (`orchestrator/execution/manifest.py:171`) only reads/parses JSON — it has no
  dependency on `config`, so this reordering is safe.
- `_load_config` (`cli.py:317-323`) already composes `apply_overrides( load_config(config_path), args)` — flag-over-config-file precedence
  (`cli.py:272-314`). The fix threads a third layer underneath: on resume, if
  the peeked manifest carries a persisted `EscalationConfig`, replace the
  config-file-loaded value's `escalation` section with it (via
  `loaded.model_copy(update={"escalation": persisted})`) *before* `apply_overrides`
  runs, so CLI flags — when the user *does* pass `--intensity` on resume — still
  win, exactly as `apply_overrides`'s existing flag > config-file > default
  chain intends; only the "default" rung under an *absent* flag changes, from
  `EscalationConfig()` to the run's own persisted config.
- Regression to fix (`tests/test_e2e_faults.py:187-190`): the existing
  workaround comment reads *"`resume` must re-state `--intensity autonomous`:
  escalation config is not persisted... the gate blocks forever on an
  escalation no one is answering"* — this is the exact defect. No test today
  exercises the actual bug (an omitted flag on resume silently reverting to
  HITL-on); add one that runs with `--intensity autonomous`, resumes with
  **no** escalation flags at all, and asserts the resumed run's effective
  config still has `intensity == "autonomous"` (e.g. via the manifest or a
  visible log line, not by re-triggering the block).

**U4 — empty error message on usage-limit failures**

- `LlmProcessError` raise site: `orchestrator/grouping/llm.py`'s
  `claude_json_runner` (~line 56, per codegraph) builds its message from
  `result.stderr.strip()[:500]` only, on any non-zero exit — a usage-limit exit
  has empty `stderr`. `SessionError` raise site:
  `orchestrator/execution/sessions.py:279`, same pattern —
  `SessionRunner._call`'s `if returncode != 0: raise SessionError(f"claude exited {returncode} ({context}): {stderr.strip()[:500]}")`.
- The useful text lives in `stdout`'s JSON envelope, but only reached on the
  **success** path today: `sessions.py:286-287` — `if envelope.get("is_error"): raise SessionError(f"claude reported an error result: {str(envelope['result'])[:500]}")` — this only runs after `returncode == 0`
  is already established (the `if returncode != 0` branch at line 278-279
  returns before ever parsing `stdout`). A usage-limit failure exits non-zero
  *and* still emits a JSON envelope with `result` populated on stdout — that
  path is currently unreached.
- Fix (same shape at both sites): on non-zero exit, attempt `json.loads(stdout)`
  in a `try/except` before building the error message; if it parses to a dict
  with a non-empty `"result"`, use that as the message body instead of
  `stderr`; if `stdout` doesn't parse or has no usable `result`, fall back to
  today's `stderr`-only message unchanged (never raise a *new* kind of error
  from inside the fallback — this is strictly additive to the message text).
  `claude_json_runner` is a bare function (not a class method) so its fix is a
  local `try/except json.JSONDecodeError` around a `json.loads(result.stdout)`
  call inserted before the existing `raise LlmProcessError(...)`.
- Verification: `tests/test_scheduler.py:298`'s `executor` stub
  (`raise LlmProcessError("claude -p failed (1): ")`) documents today's
  empty-message shape and is a fixture, not itself a test of the fix — add a
  focused unit test per raise site (e.g. in `tests/test_sessions.py` and a
  grouping-side test for `claude_json_runner`) that feeds a fake non-zero exit
  with a populated `result` field on stdout and asserts the raised message
  contains that text, plus a second test that a non-zero exit with unparseable
  stdout still falls back to the stderr-based message unchanged.

**U5 — leading space in every coder commit subject**

- Confirmed via real git history (`8f32ea1..d71514f`): 25/25 coder commits carry
  a leading space in the subject line, 0/0 orchestrator-authored merge commits
  do — ruling out a repo-side template as the source (orchestrator merge
  commits use `f"merge({self.run_id}): {group.id} {group.name}"` directly in
  `merge.py:86`, no heredoc).
  `orchestrator/prompts/coder.md` (31 lines) is the only place instructing the
  coder how to commit — its bullet: *"Commit early and often: ... make a git
  commit with a clear conventional message."* — has no formatting constraint on
  the subject line at all.
- Fix: add one explicit line to that same bullet in `coder.md`, e.g. *"the
  commit subject must start with the first character of the type, not
  whitespace — check the exact bytes if using a heredoc"* — naming the likely
  cause (a heredoc convention leaving a leading newline/space before the
  subject) so the instruction reads as a fix, not an arbitrary style rule.
  Per the user's call, no `PostToolUse` hook is added — this is a prompt-level
  root-cause fix; hooks.json (`hooks/hooks.json`) already has a `PostToolUse.Bash`
  entry, so a belt-and-suspenders trim hook remains a cheap future addition if
  the prompt fix doesn't fully stick, but rewriting a coder's own commit via
  `--amend` from a hook is a second moving part not worth adding pre-emptively
  for a cosmetic issue.
- No test today pins commit-subject formatting (git history was the only way
  this was caught). Verification is empirical, not a new unit test: after
  landing, inspect a handful of real coder commits from the next orchestrator
  run and confirm no leading whitespace.

**U3 — HITL permission-routing design proposal (no implementation)**

- Confirmed still fully unbuilt: no `PreToolUse` hook anywhere in
  `hooks/hooks.json` or `hooks/scripts/` (only `SessionStart` and
  `PostToolUse.Bash`/`PostToolUse.Edit|Write|MultiEdit` entries exist, both
  registered identically in `.claude/settings.json` for local dev per this
  repo's dual-registration rule).
- `EscalationKind` (`orchestrator/model.py:165-182`) has exactly nine kinds, all
  *report-then-resume* (a coder finishes its turn, then the orchestrator
  escalates and later resumes it) — there is no kind for a live, blocking,
  mid-turn permission decision, because the file-based
  `EscalationBroker.raise_escalation()` (`orchestrator/execution/ escalation.py:95-124`) is only ever called from the async review-loop
  coroutine (via `asyncio.to_thread`), never from inside a worker's own Claude
  Code process.
- The actual gap: today's denial path is `CoderReport.status == "permission_denied"` (`model.py:133`, `denied_command` field) — this is
  populated only *after* the coder's own prompt-level instruction
  (`coder.md`'s "retry the identical command up to three times... then stop and
  report `permission_denied`") gives up. A `PreToolUse` hook, unlike a prompt
  instruction, genuinely *can* block a tool call synchronously before it runs —
  but a hook process is short-lived and has no channel back to the long-running
  orchestrator process coordinating that group's `EscalationBroker`. This is the
  open architecture question: how does a blocking `PreToolUse` hook (running
  inside the coder's `claude -p` subprocess tree) reach the orchestrator's
  live, in-process `EscalationBroker`/`asyncio` event loop for the group it
  belongs to, given the only existing cross-process channel is the file-based
  request/response pair the broker itself already uses for report-then-resume?
- This unit's deliverable is a **written design proposal**, not code: it must
  name at least two candidate transports (e.g. reusing the existing
  `escalations_dir` file-based request/response convention with the hook
  itself polling for a response file and blocking its own exit, vs. a
  lighter-weight local socket/named-pipe per run), state which one is
  recommended and why, and enumerate the concrete follow-on units a future
  implementation plan would need (new `EscalationKind`, hook script, hook
  registration in both `hooks/hooks.json` and `.claude/settings.json` per this
  repo's dual-registration rule, broker-side listener). It explicitly does not
  implement any of them.

## Decisions

- **Merge-conflict resolve-in-place gets exactly one attempt
  (`max_conflict_resolve_attempts = 1`) before falling through to `_rewrite`.**
  Rationale: this repo defaults to `concurrency: int = 1` (serial group
  execution), so cross-group merge conflicts are already rare; a second guess
  from the same session is unlikely to out-perform a fresh spec rewrite once
  the first in-place attempt fails, and the existing rewrite path is proven.
  Alternatives rejected: matching `max_rewrites = 2` (adds a second attempt to
  a rarely-hit path for little expected benefit).

- **Persisted escalation config lives on `RunManifest`, not
  `RunState`/`state.json`.** Rationale: the manifest already holds other
  once-set, run-identity facts (`plan_path`, `base_session_id`, `grouping`)
  that don't change per-transition; `RunState` is rewritten after every group
  transition and has no natural slot for run-wide config. Alternatives
  rejected: `RunState` (co-locates with the wrong kind of data — a snapshot
  that churns, not a stable identity fact).

- **HITL permission routing (U3) ships as a design-only unit in this plan, not
  implementation.** Rationale: the transport question (how a blocking
  `PreToolUse` hook talks to a live orchestrator process) is a genuine
  architecture decision, not a bounded bug fix, and forcing an implementation
  here risks either under-designing it or stalling U1/U2/U4/U5 behind it.
  Alternatives rejected: dropping it from this plan entirely (loses the
  punch-list item's momentum and the codegraph grounding already gathered
  here); implementing a specific transport now (premature — no design review
  has happened yet).

- **U5 ships as a prompt fix only, no `PostToolUse` trim hook.** Rationale:
  root-causing at the source (`coder.md`) is simpler to verify and reversible;
  a hook that rewrites a coder's own git history via `--amend` is a second
  moving part for a cosmetic issue. Alternatives rejected: prompt fix + hook
  (belt-and-suspenders, deferred as a cheap follow-up only if the prompt fix
  doesn't fully stick in practice).

## Units

### U1. Merge-conflict resolve-in-place — try the warm coder session before rewriting

- **Goal**: On `MergeConflict`, resume the same coder session with a
  conflict-resolution prompt bounded by `max_conflict_resolve_attempts` (default
  1); only fall through to today's `_rewrite` path if that attempt itself fails
  or the cap is exhausted. `tests/test_review_loop.py:: test_merge_conflict_routes_to_rewriting_and_fans_out_a_surprise` and
  `test_rewrite_cap_fails_the_group` updated for the new order; `tests/ test_merge.py` untouched.
- **Files**: `orchestrator/execution/review.py`, `orchestrator/execution/merge.py`,
  `orchestrator/execution/prompting.py`, `orchestrator/config.py`,
  `orchestrator/prompts/conflict_resolve.md` *(new, small)*,
  `tests/test_review_loop.py`
- **Symbols**: `_GroupExecution._merge`, `_GroupExecution._rewrite`,
  `MergeConflict`, `ExecutionConfig`, `render_reentry_prompt`,
  `SessionRunner.resume`, `nudge_until_report`
- **Depends-on**: —
- **Slice**: —
- **Implements**: —
- **Consumes**: —
- **Verification**:
  - A `MergeConflict` on a group's first attempt resumes `self.coder_sid` with
    a conflict-resolution prompt (observable via the round's session id
    matching the pre-conflict coder session, not a fresh fork) before any
    `_rewrite`/spec-regeneration call happens.
  - If the resumed session's next merge attempt still conflicts (or itself
    raises `MergeConflict` again), the group falls through to `_rewrite` exactly
    as it does today — `GroupState.REWRITING` is reached and a fresh coder
    session is forked for the next generation.
  - `max_conflict_resolve_attempts` is read from `ExecutionConfig` (default
    `1`) and is overridable via `.orchestrator/config.toml`'s `[execution]`
    section, mirroring `max_rewrites`.
  - `tests/test_review_loop.py::test_merge_conflict_routes_to_rewriting_and_fans_out_a_surprise`
    and `test_rewrite_cap_fails_the_group` pass under the new resolve-then-
    rewrite order (updated to script the resolve attempt).
  - `tests/test_merge.py`'s existing `merge_group` conflict-path assertions
    are unchanged (the fix is entirely in `_GroupExecution`, not
    `IntegrationMerger`).

### U2. `resume` preserves the original run's escalation config

- **Goal**: `config.escalation` is persisted to `RunManifest` at `run` time; on
  `resume`, an omitted escalation flag (`--intensity`/`--hitl`/
  `--escalation-source`/`--escalation-timeout`) restores the persisted value
  instead of resetting to `EscalationConfig()` defaults (`on_stuck`, HITL-on).
  An explicitly-passed flag on `resume` still overrides, exactly as today.
- **Files**: `orchestrator/model.py`, `orchestrator/cli.py`
- **Symbols**: `RunManifest`, `_cmd_run`, `_load_config`, `apply_overrides`,
  `ManifestStore.load`, `EscalationConfig`
- **Depends-on**: —
- **Slice**: —
- **Implements**: —
- **Consumes**: —
- **Verification**:
  - A fresh `run` invoked with `--intensity autonomous` persists
    `escalation.intensity == "autonomous"` into `manifest.json` (readable via
    `RunManifest.model_validate_json`).
  - `resume <run_id>` invoked with **no** escalation flags at all restores
    `intensity == "autonomous"` for that run (assert via the manifest's
    persisted value or the `hitl = ...` line `_cmd_run` already prints at
    `cli.py:673-681`, not by re-triggering an actual escalation block).
  - `resume <run_id> --intensity on_stuck` (an explicit flag) overrides the
    persisted `autonomous` value for that resume, matching today's flag >
    config-file precedence.
  - A run created before this fix (a manifest with no `escalation` field, i.e.
    `escalation: None`) resumes using `EscalationConfig()` defaults exactly as
    today — no migration required, no crash on an old manifest.
  - `tests/test_e2e_faults.py`'s `resume` calls no longer need to re-state
    `--intensity autonomous` to avoid blocking (the comment at lines 187-190
    documenting the workaround is removed once the new regression test
    supersedes it).

### U3. HITL permission-routing design proposal *(design-only, no implementation)*

- **Goal**: A written design document proposing how a blocking `PreToolUse`
  hook (which can synchronously deny a tool call, unlike a prompt instruction)
  reaches the live orchestrator process coordinating that group's
  `EscalationBroker`, given today's only cross-process channel is the
  file-based request/response pair the broker already uses for
  report-then-resume escalations.
- **Files**: `docs/adr/0005-hitl-permission-routing-transport.md` *(new, small)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements**: —
- **Consumes**: —
- **Verification**:
  - The document names at least two candidate transports (e.g. hook-polls-a-
    response-file reusing `escalations_dir`, vs. a per-run local socket/named
    pipe) and states a recommendation with rationale.
  - The document enumerates the concrete follow-on units a future
    implementation plan would need: a new `EscalationKind` for a live
    permission request, the hook script itself, its registration in both
    `hooks/hooks.json` and `.claude/settings.json` (this repo's dual-
    registration rule), and the broker-side listener/handler.
  - The document contains no code changes to `orchestrator/` or `hooks/` — it
    is prose only, and explicitly states it is not implemented by this plan.

### U4. Usage-limit failures surface their real error message

- **Goal**: `LlmProcessError` (`orchestrator/grouping/llm.py`) and
  `SessionError` (`orchestrator/execution/sessions.py:279`) fall back to
  parsing `stdout`'s JSON envelope for a `result` field when `stderr` is empty
  on a non-zero exit, instead of raising with an empty message body.
- **Files**: `orchestrator/grouping/llm.py`, `orchestrator/execution/sessions.py`,
  `tests/test_sessions.py`
- **Symbols**: `claude_json_runner`, `LlmProcessError`, `SessionRunner._call`,
  `SessionError`
- **Depends-on**: —
- **Slice**: —
- **Implements**: —
- **Consumes**: —
- **Verification**:
  - A non-zero exit with empty `stderr` but a `stdout` JSON envelope
    containing `{"result": "Claude AI usage limit reached|..."}` raises with
    that `result` text present in the exception message, at both raise sites.
  - A non-zero exit with empty `stderr` and unparseable/missing-`result`
    `stdout` still raises the existing `stderr`-based message unchanged (empty
    or otherwise) — the fallback never raises a new exception type or masks
    the original failure.
  - A non-zero exit with a non-empty `stderr` and no usable `stdout` JSON
    keeps using `stderr` exactly as today (no regression to the common case).

### U5. No leading space in coder commit subjects

- **Goal**: `orchestrator/prompts/coder.md`'s commit bullet explicitly
  instructs the coder that the commit subject must not start with whitespace.
- **Files**: `orchestrator/prompts/coder.md`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements**: —
- **Consumes**: —
- **Verification**:
  - `coder.md`'s commit-early-and-often bullet contains an explicit
    no-leading-whitespace instruction for the commit subject.
  - Empirical (no automated test): after this lands, the next orchestrator run
    with real coder commits shows 0 commits with a leading space in `git log --format=%s` for that run's branches, versus the previously-confirmed
    25/25.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-merge-conflict-resolve-in-place
    description: Resume the warm coder session to resolve a merge conflict in place before falling back to spec rewriting
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/execution/merge.py
      - orchestrator/execution/prompting.py
      - orchestrator/config.py
      - orchestrator/prompts/conflict_resolve.md
      - tests/test_review_loop.py
    size_hints:
      orchestrator/prompts/conflict_resolve.md: small
    symbols:
      - _GroupExecution
      - MergeConflict
      - ExecutionConfig
      - render_reentry_prompt
    depends_on: []
    implements: []
    consumes: []
  - task_id: u2-resume-preserves-escalation-config
    description: Persist escalation config on the run manifest so resume without flags restores the original run's intensity
    slice: null
    files:
      - orchestrator/model.py
      - orchestrator/cli.py
      - tests/test_e2e_faults.py
    symbols:
      - RunManifest
      - EscalationConfig
      - apply_overrides
    depends_on: []
    implements: []
    consumes: []
  - task_id: u3-hitl-permission-routing-design
    description: Write a design proposal for how a blocking PreToolUse hook reaches the live orchestrator's escalation broker
    slice: null
    files:
      - docs/adr/0005-hitl-permission-routing-transport.md
    size_hints:
      docs/adr/0005-hitl-permission-routing-transport.md: small
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u4-usage-limit-error-messages
    description: Fall back to parsing stdout's JSON envelope for the result field when stderr is empty on a non-zero CLI exit
    slice: null
    files:
      - orchestrator/grouping/llm.py
      - orchestrator/execution/sessions.py
      - tests/test_sessions.py
    symbols:
      - claude_json_runner
      - LlmProcessError
      - SessionError
    depends_on: []
    implements: []
    consumes: []
  - task_id: u5-no-leading-space-in-commit-subjects
    description: Instruct the coder prompt template that commit subjects must not start with whitespace
    slice: null
    files:
      - orchestrator/prompts/coder.md
    symbols: []
    depends_on: []
    implements: []
    consumes: []
```
