---
title: Per-turn worker control, kernel-enforced confinement, and a legible run
type: fix
date: 2026-08-11
origin: docs/ideation/2026-08-11-orchestrator-control-isolation-and-observability-changes.md
---

# Per-turn worker control, kernel-enforced confinement, and a legible run

## Objective

Give the orchestrator the same control over a worker that an interactive session has — observe and
speak to it **per turn**, not only between rounds — then use that channel to bound context spend, and
close the isolation and legibility gaps the `obsprov1` run exposed.

Measured targets, all from run `obsprov1` (2026-08-08 → 08-11, 9 groups, 36 commits):

- No single round reaches 306k tokens again; the ladder fires at 140k/180k/200k.
- A worker cannot write to `~/.claude/projects/*/memory/` — enforced by the kernel, not by policy.
- A blocked run is visible on stdout within one line of blocking.
- A refresh conflict leaves the group `interrupted`, not terminal `failed`.
- The 902-test suite stays green.

**Self-modifying.** This plan changes `orchestrator/` itself, so every change takes effect on the
*next* run, not the one executing it (see `orchestrator/README.md`).

## What we already know (resolved context)

Everything below was verified on disk during the `obsprov1` post-mortem. Workers should not re-derive
it.

### The blocking-call root cause

`SessionRunner` is "the only module that shells the CLI" (its own docstring, `sessions.py:158`).
`_call` (`sessions.py:271`) builds argv with `--output-format json` and hands off to `_spawn`
(`sessions.py:308`), which does a single blocking `proc.communicate()` (`sessions.py:324`). Usage is
recorded only after that returns (`sessions.py:303`).

Consequences, all observed rather than theorised:

- `nudge_until_report` (`sessions.py:392`) sends follow-ups by calling `runner.resume(...)`
  (`sessions.py:248`), i.e. a **new** `claude -p --resume <sid>` process. The orchestrator can only
  speak to a session *between* rounds.
- One round ran 295 internal turns and reached **306,157** prompt tokens with `rounds_completed: 1`.
  A between-rounds check cannot see inside it.

Per-session measurements (g3):

```
coder    91d7b1f5  295 turns  peak single-turn prompt = 306,157   manifest 310,260
coder    d5ab2688  121 turns  peak =  99,152                      manifest 103,276
reviewer 97ef786c   77 turns  peak =  89,059                      manifest  91,509
```

The manifest figure equals the final turn's prompt **plus** that turn's output (2.5–4.1k), so
`RoundUsage.from_envelope` (`sessions.py:85`) is correct — it already reads `usage.iterations[-1]`
rather than the summed top-level `usage`. **Do not "fix" that**; it is the P0 repair from 2026-07-28.

### CLI flags — verified present on the installed build

`--input-format stream-json` ("realtime streaming input"), `--output-format stream-json`,
`--include-partial-messages`, `--settings <file-or-json>`, `--disallowedTools`, `--add-dir`,
`--permission-mode`.

**Absent:** there is **no `--sandbox` flag** and **no `--max-turns` flag**. Do not design around
either. `--add-dir` *widens* access and is not a restriction.

`_call` currently passes only `--permission-mode` (`sessions.py:275-276`) and optionally
`--allowedTools` (`sessions.py:277-278`). `--settings` and `--disallowedTools` are unused today.

### Confinement facts

`permission_mode` defaults to `"acceptEdits"` (`sessions.py:165`, `config.py:125`), which accepts
`Edit`/`Write` at **any** filesystem path. `~/.claude/settings.json` has 45 allow rules and **0 deny
rules**. A worker used this to write a factually false claim into the operator's global memory.

A `PreToolUse` hook only sees tool-routed writes — it cannot see `bash -c 'cat > ~/x'`, `python -c 'open(...,"w")'`, an editor, or git writing refs. `realpath` + string-prefix checks are defeated by
symlink chains, hardlinks, bind mounts, `/proc/self/root`, and TOCTOU.

Landlock is available (host kernel 6.6; Landlock needs ≥5.13), unprivileged, and inherited by every
child process. It is **allowlist-only**: a rule on a parent grants the whole subtree and there is
**no subtraction**, so "allow `~/.claude`, deny `projects/*/memory/`" is inexpressible and the
allowlist must be enumerated.

The structure makes that clean. Memory dirs live at `~/.claude/projects/<slug>/memory/` under
**operator** slugs (6 exist). Worker transcripts go to `~/.claude/projects/<worktree-slug>/`, and
**no worker slug dir has a `memory/` subdir** — verified across all of them. So allow-listing the
worker's *own* project dir excludes every memory dir by construction.

Workers still need `~/.claude` for credentials and because `transcript_root` defaults to
`Path.home()/".claude"/"projects"` (`sessions.py:179`) and the worker itself writes there. Present
subdirs: `backups cache chrome daemon downloads file-history ide jobs paste-cache plans plugins projects session-env sessions shell-snapshots skills tasks`. The Bash tool sources
`~/.claude/shell-snapshots/snapshot-bash-*.sh`.

### Git shared-state facts (documented, confirmed)

Per `git worktree` man page REFS: all `refs/` are shared **except** `refs/bisect`, `refs/worktree`,
`refs/rewritten`; pseudo-refs (`HEAD`, `ORIG_HEAD`, `MERGE_HEAD`) are per-worktree.

**`refs/stash` is shared.** A worker ran `git stash -q -u`, collided with a 2026-06-11 operator stash
on `main`, resurrected `agentmemory/cli.py` (deleted long ago in `fb2c8fa`), and killed group g1.

`create_worktree` (`worktrees.py:72`) does not set `extensions.worktreeConfig`, so **git config is
shared too** — a worker running `git config user.email` mutates the operator's repo.

`_refresh_onto_tip` (`worktrees.py:103`) uses plain `git merge` (never `--ff-only`, never rebase) and
writes only per-worktree pseudo-refs, so that path is safe as written. Its conflict branch
(`worktrees.py:118-124`) raises bare `WorktreeError`.

### State classification

`scheduler.py:445` maps `(SessionError, LlmProcessError, PermissionDenied)` → `INTERRUPTED`;
everything else falls to `except Exception` → `FAILED` (`scheduler.py:460-461`). `WorktreeError` is
not in the tuple, so a refresh conflict goes terminal even though the group's commits are valid.
`TERMINAL_STATES` is `{COMPLETED, FAILED, RESOLVED}` (`scheduler.py:100`).

### Escalation facts

`EscalationBroker.raise_escalation` (`escalation.py:97-103`) already writes `request-<id>.json`
**and** calls `log_event`. `run.log:67` carried the full escalation line. The defect is only that it
never reaches **stdout**, so an operator watching the process sees a live process and silence — the
run sat 18 hours that way. `log_event` itself needs no change.

### Bookkeeping facts

`SessionEntry` (`model.py:70`) already has `rounds_completed`, the four token counters,
`last_context_tokens`, `transcript_path`, and `retirement_reason`. They are written only on
round-completion saves via `_copy_usage` (`review.py:385`) / `_persist_coder_usage`
(`review.py:401`), which read `runner.usage_of(...)` — impossible to populate mid-round while
`_call` blocks. So `started_at` is always `None` and an in-flight group is byte-identical to one that
never started. After g1 died on a usage limit **and** was resumed, the manifest still held **one**
session entry, not two.

`record_session` (`manifest.py:196`) appends to `group.sessions`.

### Config drift

Two `.orchestrator/config.toml` files disagree: `smart_mcps` has `context_token_limit = 200000`,
`smart_mcps-fe-test` has `120000` (a week older). `.orchestrator/` is gitignored so it never
propagated. The code default is `200_000` (`config.py:112`). This did **not** cause the 306k
blowout — the breaker is read only in `_reenter` (`review.py:347`), so no value would have helped.

## Decisions

- **Bidirectional `stream-json` for the worker channel, not transcript tailing.** Tailing
  `~/.claude/projects/*/<sid>.jsonl` is read-only, so it can observe 70% but cannot say anything;
  the ladder requires speaking into a running round. `stream-json` is also a documented contract
  (the transcript schema is undocumented) and does not read from `~/.claude`, which U2 locks down.
  *Rejected:* transcript tailing as the live channel. *Retained:* tailing for sessions we do not
  spawn (the grouper) and for post-mortems — that is how the 306k figure was reconstructed.
- **Landlock, not containers.** Kernel-enforced, unprivileged, inherited by bash/python/git, no
  image build or volume plumbing. *Rejected:* containers (volume-mounting `$HOME` reopens the hole
  anyway); deny-rules alone (blind to `bash -c`).
- **Ship deny-rules *and* Landlock.** Deny-rules give clearer errors for the accidental case, which
  is the observed failure mode; Landlock is the actual boundary. Layered, not alternatives.
- **The ladder is measured against `[breaker] context_token_limit` (200,000).** 70% = 140k summary,
  90% = 180k prioritised conclusions, 100% = 200k compact report then stop. *Rejected:* a new
  budget below the window — a single request already carried 306k, so the effective window exceeds
  200k and the wrap-up turn has real headroom at this base.
- **Staged ladder, not a bare warn-then-force.** The known failure of a single forced report is that
  the wrap-up turn is itself expensive and truncates. Because the 100% report references the 70% and
  90% summaries, it is cheap.
- **Deny `git stash` to workers; no private per-worktree stash.** `refs/worktree/stash` via
  `git stash create` is documented-correct and was considered, but a worker owns a private branch and
  a WIP commit does the same job. *Accepted consequence:* `worktrees.py:128-130` still raises when a
  merge refuses on uncommitted changes; U6 makes that recoverable instead of terminal.
- **Escalations block forever; no timeout fallback.** A human always decides. Only the silence is
  fixed. *Rejected:* `on_timeout` auto-actions.
- **A token ceiling does not contradict R7.** `_spawn` has no wall-clock timeout because
  "wall-clock is a terrible proxy for stuck" (`sessions.py:322-323`). A token threshold is a proxy
  for **cost**, not for stuck. This must be stated in the code so a later reader does not revert it.

## Units

### U1. streaming-channel — make the worker channel bidirectional and per-turn

- **Goal**: `_call` consumes `--output-format stream-json --include-partial-messages` incrementally
  and can write to the child's stdin via `--input-format stream-json`, exposing a per-turn callback
  and a `send()` seam, while `RoundResult`/`RoundUsage`/`SessionUsage` keep their current meaning.
- **Files**: `orchestrator/execution/sessions.py`, `orchestrator/execution/streaming.py`
  *(new, large)*, `tests/test_streaming.py` *(new, medium)*, `tests/test_sessions.py`
- **Symbols**: `SessionRunner`, `_call`, `_spawn`, `RoundResult`, `RoundUsage`, `SessionUsage`,
  `from_envelope`, `nudge_until_report`, `usage_of`
- **Depends-on**: —
- **Slice**: session-channel
- **Implements / Consumes**: implements `worker-stream-channel`
- **Verification**:
  - A scripted fake CLI emitting a multi-turn `stream-json` stream yields a `RoundResult` whose
    `usage` equals the values from the **final** turn, matching today's `from_envelope` semantics.
  - A per-turn observer receives one callback per assistant turn, each carrying that turn's
    `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`.
  - `send()` writes a well-formed user message to the child's stdin and the fake CLI echoes having
    received it, proving the channel is bidirectional while the round is still running.
  - A non-zero child exit still raises `SessionError` whose message contains the argv context, as
    today.
  - A stream that ends without a terminal result raises `SessionError` rather than hanging.
  - The tracker still sees `spawned(pid)` and `exited(pid)` exactly once per round.
  - `uv run pytest -q` passes with no changes to `tests/test_review_loop.py`'s `StubRunner`.
- **Not touched**: `RoundUsage.from_envelope`'s `iterations[-1]` rule; the review loop's round
  numbering; `_reenter`'s breaker input; grouping.

### U2. worker-confinement — deny writes outside the worktree, enforced by the kernel

- **Goal**: every worker subprocess runs under a Landlock ruleset allowing only its worktree and an
  enumerated `~/.claude` allowlist, plus per-worker `--settings`/`--disallowedTools`; the operator's
  memory dirs are unreachable.
- **Files**: `orchestrator/execution/confinement.py` *(new, large)*,
  `orchestrator/execution/sessions.py`, `orchestrator/config.py`, `tests/test_confinement.py`
  *(new, medium)*
- **Symbols**: `SessionRunner`, `_call`, `_spawn`, `ExecutionConfig`, `SessionConfig`
- **Depends-on**: u1-streaming-channel
- **Slice**: session-channel
- **Implements / Consumes**: implements `worker-confinement`
- **Verification**:
  - With confinement enabled, a subprocess launched through `_spawn` cannot create or modify a file
    under `~/.claude/projects/<any other slug>/memory/`; the attempt fails with `EACCES`.
  - The same subprocess **can** write inside its worktree and inside
    `~/.claude/projects/<its own slug>/`.
  - The restriction survives an intermediate shell: `bash -c 'echo x > <denied path>'` also fails,
    demonstrating the boundary is inherited rather than tool-level.
  - `--disallowedTools` and `--settings` appear in the argv when configured, and are absent when not.
  - On a kernel without Landlock, confinement degrades to deny-rules only, logs one warning, and the
    round still runs — an unavailable kernel feature never fails a group.
  - The runtime-dir allowlist is **derived from an executed probe run**, and the test asserts the
    probe's recorded list is non-empty and includes `shell-snapshots`.
- **Not touched**: `permission_mode` semantics for non-worker callers; the grouper's LLM calls;
  `_scrub_virtualenv`.

### U3. context-ladder — bound context inside a round

- **Goal**: while a round runs, crossing 70%/90%/100% of `context_token_limit` sends the staged
  prompts; a round already past 100% is asked for the compact report and stopped once it delivers.
- **Files**: `orchestrator/execution/review.py`, `orchestrator/execution/sessions.py`,
  `orchestrator/config.py`, `tests/test_review_loop.py`
- **Symbols**: `BreakerConfig`, `SessionRunner`, `SessionUsage`, `RoundUsage`, `nudge_until_report`
- **Depends-on**: u1-streaming-channel
- **Slice**: round-accounting
- **Implements / Consumes**: consumes `worker-stream-channel`
- **Verification**:
  - A scripted session whose per-turn context crosses 140,000 receives exactly one summary prompt,
    and no second one while it stays between 140,000 and 180,000.
  - Crossing 180,000 sends exactly one prioritised-conclusions prompt.
  - A round sitting at 99% of the limit is **not** interrupted and produces its normal report.
  - Crossing 200,000 sends the compact-report prompt, and the round ends after the report is parsed
    rather than being killed mid-turn.
  - Each threshold fires at most once per round even when many turns exceed it.
  - With the ladder disabled in config, a session crossing every threshold receives no extra prompts,
    proving the seam defaults off.
  - `_reenter`'s existing breaker comparison against `last_context_tokens` is unchanged: a session
    persisted above the limit is still retired on re-entry.
- **Not touched**: the wall-clock-timeout decision (R7) — the code states a token ceiling is a
  proxy for cost, not for stuck; round numbering; `iterations[-1]`.

### U4. session-bookkeeping — record a session when it starts, not when it finishes

- **Goal**: `started_at`, `model`, and `transcript_path` are written at session **creation**, and
  per-turn usage lands continuously, so an in-flight group is distinguishable from one that never
  started.
- **Files**: `orchestrator/model.py`, `orchestrator/execution/manifest.py`,
  `orchestrator/execution/review.py`, `tests/test_session_bookkeeping.py` *(new, medium)*
- **Symbols**: `SessionEntry`, `record_session`, `_copy_usage`, `_persist_coder_usage`, `usage_of`
- **Depends-on**: u1-streaming-channel
- **Slice**: round-accounting
- **Implements / Consumes**: consumes `worker-stream-channel`
- **Verification**:
  - Immediately after a coder session is created and before its first round completes, the manifest
    holds an entry for it with a non-null `started_at` and a non-null `model`.
  - While a round is in flight, `rounds_completed` stays 0 but `last_context_tokens` reflects the
    latest observed turn rather than 0.
  - A session interrupted before completing any round still leaves a manifest entry carrying
    `started_at`.
  - Resuming a group after an interrupt appends a **second** session entry, so the manifest records
    two attempts where the run made two.
  - Runs recorded before these fields existed load unchanged, with `started_at` absent rather than
    raising.
- **Not touched**: `retirement_reason` semantics; the four cumulative token counters' meaning;
  artifact filenames.

### U5. git-global-state — stop workers mutating repo-global git state

- **Goal**: workers cannot run the repo-global git mutators, and each worktree gets its own git
  config so a worker's `git config` no longer edits the operator's repo.
- **Files**: `orchestrator/execution/worktrees.py`, `orchestrator/config.py`,
  `tests/test_sessions.py`
- **Symbols**: `create_worktree`, `worktree_path`, `group_branch`, `_git_ok`, `SessionConfig`
- **Depends-on**: —
- **Slice**: git-isolation
- **Implements / Consumes**: implements `worker-git-policy`
- **Verification**:
  - A worktree created by `create_worktree` reports `extensions.worktreeConfig` enabled, and setting
    `user.email` inside it leaves the parent repo's `user.email` unchanged.
  - The denied-command list rejects `git stash`, `git reset --hard`, `git clean`, `git gc`, and
    `git worktree prune`, and accepts `git status`, `git add`, `git commit`, and `git diff`.
  - `create_worktree` remains idempotent: calling it twice returns the same path on the same branch,
    as today.
  - An existing worktree on a different branch still raises `WorktreeError` naming the found branch.
- **Not touched**: `_refresh_onto_tip`'s plain-merge strategy; `remove_worktree`'s dirty refusal;
  `provision_env`.

### U6. refresh-conflict-interrupted — a merge conflict is a block, not a failure

- **Goal**: `_refresh_onto_tip`'s conflict path raises `WorktreeRefreshConflict`, which the scheduler
  classifies `INTERRUPTED`; other `WorktreeError`s stay terminal.
- **Files**: `orchestrator/execution/worktrees.py`, `orchestrator/execution/scheduler.py`,
  `tests/test_scheduler.py`
- **Symbols**: `WorktreeError`, `_refresh_onto_tip`, `create_worktree`, `GroupState`,
  `TERMINAL_STATES`
- **Depends-on**: u5-git-global-state
- **Slice**: git-isolation
- **Implements / Consumes**: consumes `worker-git-policy`
- **Verification**:
  - A group whose worktree refresh conflicts on real content ends in state `interrupted`, and its
    failure text names the conflicted paths.
  - That group is reachable by `resume` — after the conflict is resolved, resuming re-enters it and
    the group can reach `completed`.
  - A `WorktreeError` from the "path exists but is not a worktree" case still ends the group
    `failed`.
  - `TERMINAL_STATES` still excludes `interrupted`.
  - The conflict path still aborts the in-progress merge, leaving the worktree free of merge markers.
- **Not touched**: the `(SessionError, LlmProcessError, PermissionDenied)` classification; the
  resolve routine; `RESOLVED` semantics.

### U7. run-visibility — make a blocked or misconfigured run obvious

- **Goal**: a raised escalation prints to stdout naming the groups it blocks, the Observatory shows
  pending escalations, and run start echoes the resolved config path with its key values.
- **Scope note — most of the UI already exists.** `EscalationPanel.tsx` and
  `GET /api/projects/{project}/runs/{run_id}/escalations` (`observatory/escalations.py:52-56`) were
  built and tested by an earlier run and may need **no code change**; confirm before editing, and
  extend only if the pending-escalation list is not already surfaced. The genuinely new surface is
  the stdout emission on raise/answer and the config echo at run start. `log_event` is already
  correct (`escalation.py:97-103`) — do not rewrite it.
- **Files**: `orchestrator/execution/escalation.py`, `orchestrator/cli.py`,
  `orchestrator/observatory/escalations.py`, `ui/src/components/EscalationPanel.tsx`,
  `tests/test_escalation.py`
- **Symbols**: `EscalationBroker`, `raise_escalation`, `answer_escalation`, `pending_escalations`,
  `load_config`, `OrchestratorConfig`
- **Depends-on**: —
- **Slice**: run-visibility
- **Implements / Consumes**: implements `/api/projects/{project}/runs/{run_id}/escalations`
- **Verification**:
  - Raising an escalation writes a line to **stdout** containing the escalation id, its kind, and the
    group id — in addition to the existing `run.log` line, which is unchanged.
  - That stdout line names the pending groups blocked by the escalation when there are any.
  - Answering the escalation writes a second stdout line recording the action taken.
  - `GET /api/projects/{project}/runs/{run_id}/escalations` lists an unanswered escalation and omits
    one that has a matching response file.
  - Run start prints the absolute path of the config file actually loaded, together with
    `token_budget`, `context_token_limit`, and `permission_mode`.
  - Escalations still block indefinitely when no response arrives: no timeout path is introduced.
- **Not touched**: `log_event`; `EscalationConfig.timeout_s` defaults; `HumanAction` values; the
  answer CLI's arguments.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-streaming-channel
    description: Make the worker channel bidirectional and per-turn via stream-json input and output
    slice: session-channel
    files:
      - orchestrator/execution/sessions.py
      - orchestrator/execution/streaming.py
      - tests/test_streaming.py
      - tests/test_sessions.py
    size_hints:
      orchestrator/execution/streaming.py: large
      tests/test_streaming.py: medium
    symbols:
      - SessionRunner
      - RoundResult
      - RoundUsage
      - SessionUsage
      - nudge_until_report
      - _call
      - _spawn
      - from_envelope
      - usage_of
    depends_on: []
    implements: ["worker-stream-channel"]
    consumes: []
  - task_id: u2-worker-confinement
    description: Confine worker writes to the worktree with Landlock plus per-worker deny rules
    slice: session-channel
    files:
      - orchestrator/execution/confinement.py
      - orchestrator/execution/sessions.py
      - orchestrator/config.py
      - tests/test_confinement.py
    size_hints:
      orchestrator/execution/confinement.py: large
      tests/test_confinement.py: medium
    symbols:
      - SessionRunner
      - ExecutionConfig
      - SessionConfig
      - _call
      - _spawn
    depends_on: [u1-streaming-channel]
    implements: ["worker-confinement"]
    consumes: []
  - task_id: u3-context-ladder
    description: Bound context inside a round with staged prompts at 70, 90 and 100 percent
    slice: round-accounting
    files:
      - orchestrator/execution/review.py
      - orchestrator/execution/sessions.py
      - orchestrator/config.py
      - tests/test_review_loop.py
    symbols:
      - BreakerConfig
      - SessionRunner
      - SessionUsage
      - RoundUsage
      - nudge_until_report
    depends_on: [u1-streaming-channel]
    implements: []
    consumes: ["worker-stream-channel"]
  - task_id: u4-session-bookkeeping
    description: Record started_at model and transcript_path at session creation not round completion
    slice: round-accounting
    files:
      - orchestrator/model.py
      - orchestrator/execution/manifest.py
      - orchestrator/execution/review.py
      - tests/test_session_bookkeeping.py
    size_hints:
      tests/test_session_bookkeeping.py: medium
    symbols:
      - SessionEntry
      - record_session
      - _copy_usage
      - _persist_coder_usage
      - usage_of
    depends_on: [u1-streaming-channel]
    implements: []
    consumes: ["worker-stream-channel"]
  - task_id: u5-git-global-state
    description: Stop workers mutating repo-global git state and give each worktree its own config
    slice: git-isolation
    files:
      - orchestrator/execution/worktrees.py
      - orchestrator/config.py
      - tests/test_sessions.py
    symbols:
      - create_worktree
      - worktree_path
      - group_branch
      - SessionConfig
      - _git_ok
    depends_on: []
    implements: ["worker-git-policy"]
    consumes: []
  - task_id: u6-refresh-conflict-interrupted
    description: Classify a worktree refresh conflict as interrupted rather than terminal failed
    slice: git-isolation
    files:
      - orchestrator/execution/worktrees.py
      - orchestrator/execution/scheduler.py
      - tests/test_scheduler.py
    symbols:
      - WorktreeError
      - _refresh_onto_tip
      - create_worktree
      - GroupState
      - TERMINAL_STATES
    depends_on: [u5-git-global-state]
    implements: []
    consumes: ["worker-git-policy"]
  - task_id: u7-run-visibility
    description: Print raised escalations to stdout surface them in the Observatory and echo the resolved config
    slice: run-visibility
    files:
      - orchestrator/execution/escalation.py
      - orchestrator/cli.py
      - orchestrator/observatory/escalations.py
      - ui/src/components/EscalationPanel.tsx
      - tests/test_escalation.py
    symbols:
      - EscalationBroker
      - raise_escalation
      - answer_escalation
      - pending_escalations
      - load_config
      - OrchestratorConfig
    depends_on: []
    implements: ["/api/projects/{project}/runs/{run_id}/escalations"]
    consumes: []
```
