---
title: Worker friction, live breakers and the export the analyses need
type: feat
date: 2026-10-06
origin: docs/brainstorms/2026-10-06-worker-friction-and-live-breakers-requirements.md
---

# Worker friction, live breakers and the export the analyses need

## Objective

Meet R1–R15 of the origin document: remove the sandbox and prompt friction the
ten-run analysis measured (R1–R6), build the three in-round signals the data
ranked worth building plus one advisory (R7–R10), and extend the Run Bundle so
the next analysis pass measures per-turn usage, peak context, denial links and
polling instead of estimating them (R11–R15). Every number below is from
`infinity-skills/docs/research/2026-10-05-*.md` and run r20261006-050234.

## What we already know (resolved context)

- **Worker argv** is assembled in `orchestrator/execution/sessions.py:690-725`:
  `--permission-mode`, `--allowedTools` (the configured list plus
  `worktree_path_rules(...)` anchored to the worktree), `--disallowedTools`,
  `--settings <operator file>`, `--model`. `start_worker`, `start_fork` and `resume` (lines 510, 556, 616) take `extra_allowed_tools`; `start_base` (line 478, the base-context session) does not and needs no read root; `--add-dir` is
  only emitted by the fork-cwd experiment (`_fork_cwd_experiment`, line 988).
  The group executor passes `recipe.extra_allowed_tools` at every launch site
  in `orchestrator/execution/generation.py` (lines 340–720) and knows the run
  directory as `self.deps.paths.run_dir`.
- **Worker environment** is one dict per runner (`SessionRunner._env`,
  sessions.py:445) plus `launch_env(base, cwd)` per spawn (sessions.py, added
  2026-10-06), which already sets `CLAUDE_PROJECT_DIR=<worktree>/.coder-scratch`.
  Landlock's write set is built per spawn by `build_policy(worktree=cwd, …, system_paths=[*system_write_paths(), *extra_write_paths])`
  (sessions.py:925-931); `system_write_paths()` (confinement.py:172-190) adds
  the orchestrator's own `$TMPDIR` — the worker's TMPDIR is never set, so it
  defaults to `/tmp`, outside the write set.
- **Per-turn observer**: `StreamingProcess` (streaming.py) calls `on_turn(TurnUsage)`
  on every `assistant` event (line 262-270), harvests refusal text from `user`
  events' `tool_result` blocks into `deny_signals` (line 277, 308), and offers
  `on_event(event_type: str)` for liveness (line 178, 297). It exposes
  `send(text)` (line 338) for a mid-round follow-up and `close_stdin()` (line
  375); the child exits once stdin is at EOF and no follow-up is outstanding.
  `records.py:_make_coder_on_turn` (line 70-115) updates
  `entry.last_context_tokens` per turn and, when `breaker.context_ladder_enabled`,
  sends `render_ladder_summary_prompt` / `_prioritized_` / `_compact_` at
  70/90/100% of `context_token_limit`. **`context_ladder_enabled` defaults to
  `False`** (config.py:391). Retirement is decided only between rounds by
  `_breaker_reason` (generation.py:787-795).
- **Denials**: `classify_denial` (denial.py:119) attributes a `permission_denied`
  report from the report's `denied_command` / `denial_error` plus the stream's
  `deny_signals`; the ground rules already tell a coder to retry an identical
  denied command at most three times (worker_ground_rules.md:61-66). Nothing
  counts the retries on the orchestrator side.
- **Verification gate**: `_settle_round` (generation.py:416-511) runs the
  report-level gate ("completed but not every required item passes", line
  457-480) and then review; the merge gate runs the repo's check command via
  `run_preflight(worktree, *, config: PreflightConfig, output_dir, log, declared_files)` (preflight.py:382), called from merge.py:254.
- **Worktree names**: `worktree_path` (worktrees.py:153) is
  `.worktrees/<run>/<gid>-<slugify(name)>` with `slugify(max_len=40)`
  (line 148); a resumed run reads the recorded path, so a shorter cap affects
  only new worktrees.
- **Export**: `RunExport` (export.py:273, `SCHEMA_VERSION = 2`, line 51) →
  `ExportGroup` (line 182: `rewrites: list[ExportRewrite]`, `sessions`,
  `artifacts` with `surprises`, `escalations`, optional `ledger`) →
  `ExportSession` (line 71: `tokens: ExportTokens` cumulative, `cost_usd`,
  `events_path`). Events are `NeutralEvent` rows (transcript_events.py:27:
  `event_id, role, timestamp, text, tool_name, tool_use_id, tool_input, tool_output, is_error`) written by `write_events_gz`; the parser
  `_events_from_content` (line 71) reads each transcript line's content blocks
  and discards `message.usage`. `heartbeat.json` is skipped from artifacts
  (`_ARTIFACT_SKIP`, line 58). Fixture runs for export tests:
  `tests/fixtures/runs/r20260828-220035`, `r20260829-162627`.
- **Group state**: `GroupRunState` (scheduler.py:~180) persists `state, generation, failure, holds, resolve_settled, reentry_count, quarantined, cures` — no rewrite count. `_rewrite` (escalating.py:237) keeps `self.rewrites`
  in memory and logs `(spec refinement, not counted)` when uncounted.
- **Prompts**: `orchestrator/prompts/worker_ground_rules.md` (152 lines) is
  rendered into every worker prompt; `/tmp` is already forbidden (line 24-30)
  and identical-denial retries capped at three (line 61-66). Ladder prompt
  renderers live in `orchestrator/execution/prompting.py:272-286`.
- **Big test files stay out of `Files`** (`tests/test_sessions.py` 50 KB,
  `tests/test_export.py` 37 KB, `tests/test_review_loop.py`): every unit adds
  its tests in a new, small file so the grouper prices the real work.

## Decisions

- **Read roots are the worker's own run directory only, via `--add-dir`.**
  `start_fork`/`resume` gain `add_dirs`, passed by the group executor as
  `[deps.paths.run_dir]`. Rejected: every run directory plus plan-named
  siblings (the quoted refusals read other repos' runs, `/tmp` and
  `/run/user`, none of which should be granted); a generated settings file
  (the operator's `--settings` file would have to be merged).
- **`TMPDIR` and `TMP` move into `<worktree>/.coder-scratch/`** through
  `launch_env`, beside `CLAUDE_PROJECT_DIR`. The worktree is already in the
  Landlock write set, so no policy change. Rejected: adding `/tmp` to
  `additionalDirectories`.
- **The context ladder is on by default and crossing 100% ends the round.**
  The observer already exists; the flip plus one `end_round` call on the turn
  that crosses the limit is the whole mechanism. Retirement stays a
  round-boundary decision (`_breaker_reason`), now reached within one turn.
  Rejected: keeping the ladder opt-in with only a hard stop (no chance to
  compact first).
- **The stall signal warns and never retires.** On every stall window
  (K=8 observations with the same `(command string, error signature)` and no
  file, signature or verification change; 2K=16 for pure read/search; the
  `search` action class excluded from the key) the observer sends one
  "change approach" nudge. Retirement remains the token limit's job.
  Rejected: warn-then-retire (no hand-labelled data yet; a healthy long
  investigation would be killed).
- **One `RoundSignals` object per round, fed by the stream, consumed by one
  observer.** Tool calls and results are paired by `tool_use_id`; action
  classes are `read`, `edit`, `verify`, `search`, `wait`, `other`. The
  repeat-denial cap, the stall nudge, the redundant-read reminder, the
  100% stop and the verification-omission gate all read it; no second parser.
  Rejected: four independent trackers in `on_turn`.
- **The verification-omission gate is hard.** A `completed` report whose
  last `edit` has no later `verify` action runs `run_preflight` on the
  worktree before review; a red result becomes a `changes_required` round
  carrying the failing output. Rejected: warn-only for the first run.
- **The third identical denied command ends the round.** The observer sends
  "stop retrying; report `permission_denied` with this exact command" and
  closes stdin; the existing `classify_denial` path then attributes it.
  Rejected: counting in `denial.py` after the report (the cost is the
  retries, which are over by then).
- **Export is additive at `schema_version` 3.** Per-event `usage` on
  assistant events, `peak_context_tokens` and `last_context_tokens` per
  session, group phase timeline from `run.log` anchors, `denial_event_id`
  on a `permission_denied` artifact, `session_id`/`seq` on surprises, and a
  `wait` tag on polling commands. A bundle built from an old run omits what
  it cannot derive; `ingest` of a v2 bundle is unchanged. Escalations already export per group as `ExportEscalation` with their ids; giving them a `seq` is out of scope (R13's escalation half).
- **Worktree slugs are capped at 24 characters.** Cheap, and the only lever
  on path length the orchestrator owns; cause is low-confidence (R6).
- **The rewrite counter is persisted in `state.json`**, with whether the
  last rewrite was counted; exported on the group.
- No ADRs: every decision above is a config default, a prompt line or an
  additive field, all reversible.

## Units

### U1. read-roots — the worker's own run directory is a trusted read root

- **Summary**: [R1] A worker is launched with `--add-dir <repo>/.orchestrator/runs/<run_id>` so a compound Bash command that reads its own run's `run.log` or group reports is no longer refused; no other directory is added.
- **Goal**: `SessionRunner.start_worker`, `start_fork` and `resume` accept `add_dirs: Sequence[Path] = ()` and emit one `--add-dir <path>` per entry after the allowlist flags; the group executor passes `[self.deps.paths.run_dir]` at every coder, reviewer and resume launch site in `generation.py`; the reviewer launch (`reviewer.py:56`, `runner.resume(...)`) passes the same. `_argv_context` names `--add-dir` in the context line so the launch log shows it.
- **Recipe**: —
- **Files**: `orchestrator/execution/sessions.py`, `orchestrator/execution/generation.py`, `orchestrator/execution/reviewer.py`, `tests/test_read_roots.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - Run: `uv run pytest tests/test_read_roots.py -q` Pass: green — a stub-CLI coder launch's recorded argv contains exactly one `--add-dir` whose value is the run directory, and a launch with `add_dirs=()` contains none.
  - Run (driver): `uv run pytest tests/test_read_roots.py -m llm -q --basetemp=.orchestrator/live-u1` Pass: a confined real worker running `ls .orchestrator/runs/<run>/ && echo ok` in a compound command prints `ok` with no "requires approval" refusal in its stream; the same worker's `ls /tmp && echo ok` is still refused.
  - Run: `uv run ruff check orchestrator/execution/sessions.py orchestrator/execution/generation.py tests/test_read_roots.py` Pass: no findings.

### U2. tmpdir-and-shell-rules — temp files land in the worktree and the three refused shell forms are banned by rule

- **Summary**: [R2, R3, R4] Every worker spawn sets `TMPDIR` and `TMP` to `<worktree>/.coder-scratch/`, and the ground rules forbid `cd X && git`, `$(…)`/brace/newline-`#` inside quoted arguments, and an `Edit` whose `old_string` was not copied from an immediately preceding `Read`.
- **Goal**: `launch_env` (sessions.py) adds `TMPDIR` and `TMP` beside `CLAUDE_PROJECT_DIR`, pointing at `.coder-scratch/`, which it creates if absent; `orchestrator/prompts/worker_ground_rules.md` gains, under "For coders", one bullet per rule: `git -C <absolute path>` instead of `cd X && git` (the CLI refuses the trust change); no `$(…)`, brace expansion or a newline followed by `#` inside a quoted argument (static-analysis refusals `simple_expansion`, "Newline followed by #"); multi-line Python in a file under `.coder-scratch/`; and "`Read` the exact span before every `Edit`; copy `old_string` from that output, never from a diff or from memory".
- **Recipe**: —
- **Files**: `orchestrator/execution/sessions.py`, `orchestrator/prompts/worker_ground_rules.md`, `tests/test_launch_env_rules.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - Run: `uv run pytest tests/test_launch_env_rules.py -q` Pass: green — `launch_env(base, cwd)` returns `TMPDIR == TMP == <cwd>/.coder-scratch` and leaves `base` untouched; the rendered worker prompt contains the strings `git -C`, `simple_expansion` and `never from a diff`.
  - Run (driver): `uv run pytest tests/test_launch_env_rules.py -m llm -q --basetemp=.orchestrator/live-u2` Pass: a confined real worker running `mktemp` prints a path under its worktree's `.coder-scratch/`, and `mktemp -d && echo ok` is not refused.
  - Run: `uv run ruff check orchestrator/execution/sessions.py tests/test_launch_env_rules.py` Pass: no findings.

### U3. short-worktree-slugs — group worktree names are capped at 24 characters

- **Summary**: [R6] New worktrees are created at `.worktrees/<run>/<gid>-<slug>` with the slug capped at 24 characters, so every path argument a worker writes is shorter; resumed runs keep their recorded paths.
- **Goal**: `slugify(name, max_len=24)` is the default used by `worktree_path`; the cap is one named constant; a worktree recorded in `state.json` or the manifest under a longer name is still resolved by its recorded path on resume (the existing resolve-by-branch path, `worktrees.py:196`).
- **Recipe**: —
- **Files**: `orchestrator/execution/worktrees.py`, `tests/test_worktrees.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - Run: `uv run pytest tests/test_worktrees.py -q` Pass: green — `worktree_path(repo, "r1", "g1", "<60-character name>")` ends in a slug of at most 24 characters with no trailing `-`, and the `integration` worktree path is unchanged.
  - Run: `uv run python -c "from orchestrator.execution.worktrees import worktree_path; from pathlib import Path; print(worktree_path(Path('/r'), 'r1', 'g2', 'evidence-run the report md evidence section rendered over the real run set'))"` Pass: prints `/r/.worktrees/r1/g2-evidence-run-the-report` (the 24-character cut ends in `-`, which is stripped).
  - Run: `uv run ruff check orchestrator/execution/worktrees.py tests/test_worktrees.py` Pass: no findings.

### U4. round-signals — one per-round reading of tool calls, results, action classes and repeats

- **Summary**: `orchestrator/execution/round_signals.py` turns a round's stream events into one `RoundSignals` object — paired tool calls and results, action classes (`read`, `edit`, `verify`, `search`, `wait`, `other`), touched files, identical-denial counts, stall windows, redundant reads and "last edit has no later verify" — fed by a new full-event hook on `StreamingProcess`.
- **Goal**: `StreamingProcess` gains `on_tool_event: Callable[[dict], None] | None`, called for every `assistant` and `user` event with the raw event (never raising into the reader, like `on_event`). `RoundSignals.observe(event)` pairs `tool_use` blocks with their `tool_result` by `tool_use_id`; classifies each call: `Read`/`Glob` → `read`, `Edit`/`Write`/`MultiEdit`/`NotebookEdit` → `edit`, `Grep`/`Bash` whose program is `ls`/`find`/`grep`/`rg` → `search`, `Bash` matching a configurable verify regex (`pytest|tox|nox|vitest|jest|mocha|ruff|mypy|tsc|pyright|flake8|eslint|pre-commit|cargo test|go test|npm test|unittest`, the same set `derive_action_type` uses in infinity-skills) → `verify`, `Bash` matching `kill -0|sleep |tail -f|jobs|wait |nohup|ps ` → `wait`, else `other`. It exposes: `identical_denials(command) -> int` (count of `tool_result` with `is_error` whose text matches the stream's deny patterns for the same normalised command); `stall_window() -> StallWindow | None` (the last K calls share one `(command string, error signature)` with no new touched file, no signature change and no `verify` result change; K from config, 2K when every call in the window is `read`/`search`; `search` calls never form a window); `redundant_read(path) -> int | None` (the turn index of the earlier `Read` of the same path with no `edit` of it in between, else None, at most once per path); `last_edit_unverified() -> bool`; `turn_index`. Error signature is `<tool>:<first error line, lowercased, digits stripped>`. `RoundResult` (sessions.py:362) gains `signals: RoundSignals | None = None`, the carrier every later reader uses.
- **Recipe**: —
- **Files**: `orchestrator/execution/round_signals.py` *(new, large)*, `orchestrator/execution/streaming.py`, `tests/test_round_signals.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: `RoundSignals` / —
- **Verification**:
  - Run: `uv run pytest tests/test_round_signals.py -q` Pass: green — on a synthetic event list: eight identical failing `Bash` calls with one error text yield one stall window and `identical_denials` of 0; the same with deny text yields `identical_denials == 8`; twelve `ls` calls of distinct paths yield no window; `Read a, Read a` yields `redundant_read("a") == 0` and `Read a, Edit a, Read a` yields None; `Edit a, Bash pytest, report` yields `last_edit_unverified() is False` and `Edit a, Bash ruff check` also False; `Edit a, Read b` yields True.
  - Run: `uv run pytest tests/test_streaming.py -q` Pass: green — the existing stream tests are unchanged and a new case shows `on_tool_event` receives both the `assistant` tool_use event and the `user` tool_result event of one fake-CLI round, and a hook that raises does not stop the stream.
  - Run: `uv run ruff check orchestrator/execution/round_signals.py orchestrator/execution/streaming.py tests/test_round_signals.py` Pass: no findings.

### U5. turn-observer — the ladder on by default, the 100% stop, the repeat-denial stop, the stall nudge and the redundant-read reminder

- **Summary**: [R5, R7, R8, R10] `_make_coder_on_turn` reads the round's `RoundSignals` and, with the context ladder now on by default, ends the round within one turn of crossing `context_token_limit`, ends it on the third identical denied command, sends one "change approach" nudge per stall window, and sends one reminder per redundantly re-read file.
- **Goal**: `BreakerConfig.context_ladder_enabled` defaults to `True`; new fields `stall_window_k: int = 8` (pure read/search windows use `2 * stall_window_k`) and `repeat_denial_cap: int = 3`. The coder round call passes an `end_round: Callable[[str], None]` beside `send` — it sends the given text as a final follow-up and closes stdin so the child exits after that turn (`sessions.py` wiring around the `on_turn` lambda, line 948) — and installs the round's `RoundSignals` as the stream's `on_tool_event`. The same object is attached to the round's `RoundResult.signals` when the round settles, so `_settle_round` reads it without a second parser. In `on_turn`: at ≥ 100% of the limit, after the compact prompt, `end_round(render_limit_stop_prompt())` once; when `identical_denials(cmd) >= repeat_denial_cap` for the call just observed, `end_round(render_repeat_denial_prompt(cmd))` once; when `stall_window()` is new (its start index was not nudged yet), `send(render_stall_nudge_prompt(window))`; when `redundant_read(path)` returns a turn index, `send(render_redundant_read_prompt(path, turn))`. Every send and end logs one `run.log` line (`group <gid> generation <n>: <signal> at turn <t> — <detail>`). The four prompt renderers live in `prompting.py`. `_breaker_reason` is unchanged: a round ended by the 100% stop is retired there as today.
- **Recipe**: —
- **Files**: `orchestrator/execution/records.py`, `orchestrator/execution/prompting.py`, `orchestrator/config.py`, `orchestrator/execution/sessions.py`, `tests/test_turn_observer.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U4
- **Slice**: —
- **Implements / Consumes**: — / `RoundSignals`
- **Verification**:
  - Run: `uv run pytest tests/test_turn_observer.py -q` Pass: green — with the stub runner's per-turn sequences: a round whose fourth turn reports context above the limit receives the compact prompt and then exactly one stop follow-up, and the stream's stdin is closed after it; three identical denied `Bash` calls produce one repeat-denial follow-up and a closed stdin, two do not; eight identical failing calls produce one stall nudge and sixteen produce two; `Read a, Read a` produces one reminder and a third read none; each event writes its `run.log` line.
  - Run: `uv run pytest tests/test_review_loop.py -q -k "ladder or context"` Pass: green — the existing ladder tests pass with the default flipped (a test that asserted the off-by-default behaviour is updated to assert on-by-default, and one asserts `context_ladder_enabled = false` in config still disables every prompt).
  - Run: `uv run python -c "from orchestrator.config import BreakerConfig; b = BreakerConfig(); print(b.context_ladder_enabled, b.stall_window_k, b.repeat_denial_cap)"` Pass: prints `True 8 3`.
  - Run: `uv run ruff check orchestrator/execution/records.py orchestrator/execution/prompting.py orchestrator/config.py orchestrator/execution/sessions.py tests/test_turn_observer.py` Pass: no findings.

### U6. verification-omission-gate — a report whose last edit was never followed by a verify command runs the check command before review

- **Summary**: [R9] A `completed` coder report is accepted for review only after the repo's `check_command` has run on the worktree when the round's signals show the last `edit` had no later `verify` action; a red result becomes a `changes_required` round carrying the failing output.
- **Goal**: In `_settle_round`, after the report-level verification gate and before `_review_round`, if `result.signals is not None and result.signals.last_edit_unverified()` is True and `config.preflight.check_command` resolves, call `run_preflight(worktree, config=…, output_dir=<group dir>/omission-check-r<N>/, log=…)`; log `group <gid> generation <n> round <r>: verification omitted after the last edit — running check command`; on failure, synthesise a `ReviewerVerdict(status="changes_required", required_changes=[<first failing test or the tail of the log>], notes="verification omitted …")` and continue into the existing changes_required path (breaker gate, next-round prompt) without spending a reviewer session; on success, log `… check command green` and proceed to review as today. A reviewer session or a non-`code` recipe is never gated.
- **Recipe**: —
- **Files**: `orchestrator/execution/generation.py`, `tests/test_verification_gate.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: U4, U5
- **Slice**: —
- **Implements / Consumes**: — / `RoundSignals`
- **Verification**:
  - Run: `uv run pytest tests/test_verification_gate.py -q` Pass: green — a stub round `Edit a, Read b, report completed` with a check command that fails ends as `changes_required` with the failing output in `required_changes` and no reviewer session spawned; the same with a green check command reaches the reviewer; `Edit a, Bash pytest -q, report completed` never runs the check command; the `run.log` carries the two anchor lines.
  - Run: `uv run pytest tests/test_review_loop.py -q -k "settle or approved"` Pass: green — existing settle-round tests are unchanged (their stub rounds carry a verify action or no edit).
  - Run: `uv run ruff check orchestrator/execution/generation.py tests/test_verification_gate.py` Pass: no findings.

### U7. export-usage-and-context — per-event usage, peak context and the group phase timeline in the bundle

- **Summary**: [R11, R12] Every assistant event in `events/<session>.jsonl.gz` carries `usage {input, output, cache_read, cache_creation}`, every session carries `peak_context_tokens` and `last_context_tokens`, every group carries a `phases` timeline from `run.log`, and `ingest.json` is `schema_version` 3.
- **Goal**: `NeutralEvent` gains `usage: ExportUsage | None` (transcript_events.py), filled by `_events_from_content` from the transcript line's `message.usage` for `assistant` rows and left None elsewhere; `SessionEntry` (model.py) gains `peak_context_tokens: int = 0`, updated by the per-turn bookkeeping in `records.py` as `max(peak, context)` and persisted with `last_context_tokens`; `ExportSession` gains both fields (0 when unrecorded); `ExportGroup` gains `phases: list[ExportPhase]` (`{phase, at}`) parsed from the group's `run.log` anchors (`worktree ready`, `round N: started`, `round N: ended (<status>)`, `merge attempt`, `merged into the integration branch`, `completed` / `failed`); `SCHEMA_VERSION = 3`; a bundle built from a run without the facts omits nothing and fills zeros/empties.
- **Recipe**: —
- **Files**: `orchestrator/execution/transcript_events.py`, `orchestrator/execution/export.py`, `orchestrator/execution/records.py`, `orchestrator/model.py`, `tests/test_export_v3.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: export-v3
- **Implements / Consumes**: `bundle-v3` / —
- **Verification**:
  - Run: `uv run pytest tests/test_export_v3.py -q -k usage` Pass: green — `build_export(paths, project=…, events_dir=…, transcript_root=<tmp>)` over a synthetic transcript tree (the way `tests/test_export.py` builds one: two `assistant` lines carrying `message.usage`, one `user` tool_result line) yields `schema_version == 3`, a four-field `usage` on exactly the two assistant events and `None` elsewhere, and a session whose `peak_context_tokens >= last_context_tokens` is read from a manifest entry carrying both.
  - Run: `uv run pytest tests/test_export_v3.py -q -k phases` Pass: green — `build_export` over `RunPaths(repo_root, 'r20260828-220035', run_dir=Path('tests/fixtures/runs/r20260828-220035'))` with no transcripts yields, for every group, a non-empty `phases` list parsed from the tracked `run.log`, in log order, starting with a `worktree ready` or `round 1: started` entry and ending with `completed` or `failed`; sessions export with `peak_context_tokens == 0` (unrecorded) and nothing raises.
  - Run: `uv run python -c "from pathlib import Path; from orchestrator.execution.export import build_export; from orchestrator.execution.manifest import RunPaths; e = build_export(RunPaths(Path('.'), 'r20260828-220035', run_dir=Path('tests/fixtures/runs/r20260828-220035')), project='fx'); print(e.schema_version, [(g.id, [ph.phase for ph in g.phases][:3]) for g in e.groups][:2])"` Pass: prints `3` and, for the first two groups, three phase names each taken from the tracked fixture `run.log` (a real artifact of run r20260828-220035, not written by this unit).
  - Run: `uv run pytest tests/test_export.py -q` Pass: green — every existing export assertion still holds (additive fields only).
  - Run: `uv run ruff check orchestrator/execution/transcript_events.py orchestrator/execution/export.py orchestrator/execution/records.py orchestrator/model.py tests/test_export_v3.py` Pass: no findings.

### U8. export-links-and-wait — denials and surprises linked to their events, polling tagged

- **Summary**: [R13, R14] A `permission_denied` artifact in the bundle names the event id of the denied tool call (`denial_event_id`), every exported surprise carries the `session_id` and `seq` of the report it came from, and every `Bash` event whose command is process polling carries `tags: ["wait"]`.
- **Goal**: `NeutralEvent` gains `tags: list[str]` (empty by default); `_events_from_content` tags a `Bash` tool call `wait` when its command matches the same polling regex `RoundSignals` uses (one shared constant in `round_signals.py` imported by `transcript_events.py`); `ExportArtifact` gains `denial_event_id: str | None`, filled for a `permission_denied` coder report by locating, in that session's parsed events, the last `Bash` tool call whose `command` equals the report's `denied_command` (whitespace-normalised); `ExportSurprise` gains `session_id: str | None` and `seq: int` (the surprise's index within its report).
- **Recipe**: —
- **Files**: `orchestrator/execution/export.py`, `orchestrator/execution/transcript_events.py`, `tests/test_export_v3.py`
- **Symbols**: —
- **Depends-on**: U4, U7
- **Slice**: export-v3
- **Implements / Consumes**: — / `bundle-v3`
- **Verification**:
  - Run: `uv run pytest tests/test_export_v3.py -q -k "denial or wait or surprise"` Pass: green — a fixture run with a `permission_denied` report exports `denial_event_id` equal to the event id of the matching `Bash` call; a transcript with `kill -0 123` and `sleep 5` yields two events tagged `wait` and a `pytest -q` event with no tag; every exported surprise has a `session_id` and consecutive `seq` within its report.
  - Run: `uv run python -c "from orchestrator.execution.round_signals import WAIT_COMMAND_RE as W; print([bool(W.search(c)) for c in ['kill -0 48389', 'sleep 5; tail -f .orchestrator/runs/r1/logs/run.log', 'uv run pytest -q', 'nohup smart-mcps-orchestrate run --repo . &', 'ls -la data/']])"` Pass: prints `[True, True, False, True, False]` — the shared constant classifies the real polling commands drivers and workers issued on run r20261006-050234 as `wait` and leaves a test run and a listing untagged.
  - Run: `uv run ruff check orchestrator/execution/export.py orchestrator/execution/transcript_events.py tests/test_export_v3.py` Pass: no findings.

### U9. rewrite-counter — the rewrite count and whether it was charged are persisted and exported

- **Summary**: [R15] `state.json` carries each group's `rewrites` count and `last_rewrite_counted`, written by `_rewrite`, shown by `status`, and exported on the group.
- **Goal**: `GroupRunState` gains `rewrites: int = 0` and `last_rewrite_counted: bool | None = None`; `_rewrite` updates both through the group context (`ctx`) on every rewrite — counted or not — and `status` prints `rewrites: N (last: counted|spec refinement)` on a group with any; `ExportGroup` gains `rewrite_count: int` and `last_rewrite_counted: bool | None` read from `state.json` (0/None for old runs). The in-memory `self.rewrites` the cap check uses is initialised from the persisted value on resume, so a resumed group no longer forgets rewrites spent before the restart.
- **Recipe**: —
- **Files**: `orchestrator/execution/scheduler.py`, `orchestrator/execution/escalating.py`, `orchestrator/execution/export.py`, `tests/test_rewrite_counter.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: U7
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - Run: `uv run pytest tests/test_rewrite_counter.py -q` Pass: green — a stub group rewritten once by a blocked report persists `rewrites == 1, last_rewrite_counted is True` in `state.json`; a pre-launch refinement-only rewrite persists `rewrites == 1, last_rewrite_counted is False`; a resume after a counted rewrite starts `self.rewrites` at 1; the export of that run carries `rewrite_count == 1`.
  - Run: `uv run pytest tests/test_rewrite_observability.py tests/test_spec_refinement.py -q` Pass: green — existing rewrite tests unchanged.
  - Run: `uv run pytest tests/test_rewrite_counter.py -q -k status` Pass: green — the test copies `tests/fixtures/runs/r20260828-220035` into `<tmp>/.orchestrator/runs/`, sets `rewrites = 2` and `last_rewrite_counted = False` on one group in that copy's `state.json`, runs `smart-mcps-orchestrate status r20260828-220035 --repo <tmp>` as a subprocess, and asserts the output carries `rewrites: 2 (last: spec refinement)` for that group and no rewrites line for a group left at 0 — the oracle is the real CLI over a real run's state file.
  - Run: `uv run ruff check orchestrator/execution/scheduler.py orchestrator/execution/escalating.py orchestrator/execution/export.py tests/test_rewrite_counter.py` Pass: no findings.

## Task Map

```yaml
# orchestrator-task-map v2
tasks:
  - task_id: u1-read-roots
    description: Launch every worker with --add-dir for its own run directory so compound reads of run.log and group reports are not refused
    slice: null
    files:
      - orchestrator/execution/sessions.py
      - orchestrator/execution/generation.py
      - orchestrator/execution/reviewer.py
      - tests/test_read_roots.py
    size_hints:
      tests/test_read_roots.py: small
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u2-tmpdir-and-shell-rules
    description: Point TMPDIR and TMP at the worktree's .coder-scratch and add the shell-form and Edit-from-Read ground rules
    slice: null
    files:
      - orchestrator/execution/sessions.py
      - orchestrator/prompts/worker_ground_rules.md
      - tests/test_launch_env_rules.py
    size_hints:
      tests/test_launch_env_rules.py: small
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u3-short-worktree-slugs
    description: Cap group worktree slugs at 24 characters so worker path arguments stay short
    slice: null
    files:
      - orchestrator/execution/worktrees.py
      - tests/test_worktrees.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u4-round-signals
    description: One per-round RoundSignals reading of tool calls, results, action classes, identical denials, stall windows, redundant reads and unverified edits, fed by a full-event stream hook
    slice: null
    files:
      - orchestrator/execution/round_signals.py
      - orchestrator/execution/streaming.py
      - tests/test_round_signals.py
    size_hints:
      orchestrator/execution/round_signals.py: large
      tests/test_round_signals.py: medium
    symbols: []
    depends_on: []
    implements: ["RoundSignals"]
    consumes: []
  - task_id: u5-turn-observer
    description: Context ladder on by default with a stop at 100 percent, a stop on the third identical denial, a stall nudge per window and a redundant-read reminder, all from the per-turn observer
    slice: null
    files:
      - orchestrator/execution/records.py
      - orchestrator/execution/prompting.py
      - orchestrator/config.py
      - orchestrator/execution/sessions.py
      - tests/test_turn_observer.py
    size_hints:
      tests/test_turn_observer.py: medium
    symbols: []
    depends_on: [u4-round-signals]
    implements: []
    consumes: ["RoundSignals"]
  - task_id: u6-verification-omission-gate
    description: Run the check command before review when a completed report's last edit had no later verify action, turning a red result into a changes_required round
    slice: null
    files:
      - orchestrator/execution/generation.py
      - tests/test_verification_gate.py
    size_hints:
      tests/test_verification_gate.py: small
    symbols: []
    depends_on: [u4-round-signals, u5-turn-observer]
    implements: []
    consumes: ["RoundSignals"]
  - task_id: u7-export-usage-and-context
    description: Per-event usage, per-session peak and last context, and a group phase timeline in the export bundle at schema_version 3
    slice: export-v3
    files:
      - orchestrator/execution/transcript_events.py
      - orchestrator/execution/export.py
      - orchestrator/execution/records.py
      - orchestrator/model.py
      - tests/test_export_v3.py
    size_hints:
      tests/test_export_v3.py: medium
    symbols: []
    depends_on: []
    implements: ["bundle-v3"]
    consumes: []
  - task_id: u8-export-links-and-wait
    description: Link permission_denied artifacts and surprises to their events and tag polling commands wait in the export
    slice: export-v3
    files:
      - orchestrator/execution/export.py
      - orchestrator/execution/transcript_events.py
      - tests/test_export_v3.py
    symbols: []
    depends_on: [u4-round-signals, u7-export-usage-and-context]
    implements: []
    consumes: ["bundle-v3"]
  - task_id: u9-rewrite-counter
    description: Persist each group's rewrite count and whether the last rewrite was charged, show it in status and export it
    slice: null
    files:
      - orchestrator/execution/scheduler.py
      - orchestrator/execution/escalating.py
      - orchestrator/execution/export.py
      - tests/test_rewrite_counter.py
    size_hints:
      tests/test_rewrite_counter.py: small
    symbols: []
    depends_on: [u7-export-usage-and-context]
    implements: []
    consumes: []
```
