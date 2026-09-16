---
title: Stall detection and decision carry
type: feat
date: 2026-09-16
origin: docs/brainstorms/2026-09-16-stall-detection-and-decision-carry-requirements.md
---

# Stall detection and decision carry

## Objective

Give every running group a **Sign of Life** read from the worker child itself
(R1, R2), bounded by a **Liveness Window** that caps signal age and never work
(R3), and report **Not Live** with evidence in the run log, `status` and the
Observatory (R4) under phase names that say what is happening (R5). Record a
machine suspend as a fact (R6) and cure a child that has shown no Sign of Life
since the wake, in-process, counted and capped, never as a Re-entry (R7, R8).
Show the worker's last actions in `status` and the Observatory (R9) and stop
the run-driver skill from guessing (R10). Turn every `coder_question` answer
into an **Operator Decision** derived from the escalation records (R11),
binding by default (R12), carried verbatim into every later coder, handoff,
reviewer and speccer prompt of the group (R13) and treated by the reviewer as
a spec amendment (R14). Route a content-filter error as its own failure kind
(R15) and make `retry` leave no stale failure line (R16). Evidence is unit
tests, one live-tier test, and one real learning_podcast run driven by the
run-driver skill (R17).

R17's third half — the learning_podcast run — is not a unit: it is the
acceptance step `/orchestrator-run` performs on `~/wksp/learning_podcast` after
this plan merges and plugin 0.18.0 is installed there. Its notes file must show
a `status` liveness line, an activity tail, and one `coder_question` answer
appearing verbatim in a later generation's reviewer prompt.

## What we already know (resolved context)

- **Heartbeat.** `orchestrator/execution/heartbeat.py` — `RoundHeartbeat` is
  one daemon thread per group (`DEFAULT_INTERVAL_SECONDS = 15.0`), writes
  `<run>/groups/<gid>/heartbeat.json` through `snapshot()`, and already exposes
  a per-tick hook `on_tick` (used by `_watch_transcript` in `review.py`).
  `mark_phase` writes immediately; `mark_round` calls `mark_phase("running")`.
  `read_heartbeat(paths, gid)` is the tolerant reader every consumer uses.
  `tests/test_heartbeat.py` has `FORBIDDEN_KEYS = ("stalled", "stall", "hung", "hang", "stuck", "is_alive", "healthy")` — new facts must not use them.
  The run-scoped heartbeat (`group_id=None`, file `<run>/heartbeat.json`) is
  only started on the legacy fork path (`cli.py` ~2159); post-ADR-0007 runs
  never write it, so it is free for run-level suspend facts.
- **Stream reader.** `orchestrator/execution/streaming.py` —
  `StreamingProcess._read_stdout` parses every stream-json line and branches on
  `assistant` / `user` / `result`; there is no per-event hook. `wait()` blocks
  in `proc.wait()`. The child is spawned by plain `Popen` with a Landlock
  `preexec_fn` and **no `start_new_session`**, so it shares the driver's
  process group. The module docstring and `sessions.py` carry the R7
  no-timeout note ("A hung stream is only detected by the process itself
  exiting or by the caller closing stdin/killing it").
- **Session runner.** `orchestrator/execution/sessions.py` — `_spawn` builds
  the `StreamingProcess` with `tracker=self.tracker` (`spawned(pid, context)`
  / `exited(pid)`); `context` is `--session-id <sid>` or `--resume <sid>`
  (`_argv_context`). `_invoke` raises `SessionError(f"claude reported an error result: …")` on an `is_error` envelope (line ~775) — that is where `API Error: Output blocked by content filtering policy` lands today — and
  `SessionError(f"claude exited …: {detail}")` on a nonzero exit, with
  `UsageLimit` / `AuthExpired` as typed subclasses (`is_usage_limit`,
  `is_auth_error`). `start_worker` always passes `--session-id`; the reviewer's
  first launch (`review.py` `_review_round`) gets a runner-generated id the
  loop does not know until the round returns. `nudge_until_report` calls
  `runner.resume` internally.
- **Review loop.** `orchestrator/execution/review.py` — every worker call is
  `await asyncio.to_thread(runner.X, …)`: first launch (`_launch_call`,
  ~530), revision resume (~661), reviewer launch/re-review (~940/~955),
  answer resume (`_resolve_needs_input`, ~1441), re-entry (`_reenter`, ~731,
  which catches `SessionError` and falls back to a fresh fork). Phases are
  marked at ~517 (`starting the coder` / `forking the base session`), ~729
  (`resuming the interrupted coder`), ~545 (`coder working toward a report`),
  ~936 (`reviewer verifying the report`), ~1023 (`merging into integration`).
  `_prepare_handoff` (~1400) passes reviewer items + `_grant_notes` only.
  `_operator_notes` is the one-shot `## Operator note` (`_apply_operator_note`).
  `_rewrite` calls `deps.rewrite_spec(group, surprises)`; the provider is
  `_rewrite_provider` in `cli.py` (~2673), whose skeleton is
  `{tasks, files, previous_spec, rewrite_context}` rendered through
  `orchestrator/prompts/rewrite_speccer.md`.
- **Prompts.** `orchestrator/execution/prompting.py` — `render_coder_prompt`,
  `render_handoff_prompt`, `render_reviewer_prompt`, `render_re_review_prompt`
  use `string.Template` over `orchestrator/prompts/*.md` (`$placeholders`).
  `reviewer.md` judges "whether the work satisfies the <spec> above"; no
  template mentions operator decisions.
- **Escalations.** `orchestrator/execution/escalation.py` —
  `answer_escalation(paths, esc_id, action, text)` writes
  `response-<id>.json` (`EscalationResponse`: `id`, `action`, `answer`,
  `answered_at` — `orchestrator/model.py` ~382); both the CLI `answer` and the
  Observatory `post_answer` (`orchestrator/observatory/escalations.py`) call
  it. Request/response pairs live under `<run>/escalations/` **and**
  `<run>/groups/<gid>/` (`export.py` `_escalations_by_group` globs both).
  `EscalationRequest` carries `kind`, `group_id`, `generation`, `prompt`,
  `created_at`. `EscalationKind.CODER_QUESTION` / `CODER_BLOCKED` exist.
- **Scheduler / state.** `orchestrator/execution/scheduler.py` —
  `GroupRunState` (`state`, `generation`, `failure`, `holds`,
  `reentry_count`, `quarantined`); `set_state(gid, RUNNING)` already clears
  `failure` (so R16 is only the `retry` text); `GroupContext(group, generation, set_state, set_generation)`; `LivePid` + `_proc_starttime`
  (field 22 of `/proc/<pid>/stat`, split after the `)`); `_terminate(pid)`
  (SIGTERM → 2 s → SIGKILL); `_SchedulerPidTracker`. `RUN_STATE_SCHEMA_VERSION = 1` — adding a defaulted field needs no bump.
- **Driver.** `orchestrator/execution/driver.py` — `DriverLock` holds the
  `flock` for the driver's lifetime and runs a record thread
  (`RECORD_INTERVAL_SECONDS = 10`); `driver_status_line` says `progressing`
  from `newest_heartbeat_mtime` alone (`STALE_HEARTBEAT_SECONDS = 120`).
  The SIGINT handler (`cli.py` `_cancelling_handler`) kills no children.
- **CLI.** `orchestrator/cli.py` — `_cmd_status` (~2847) prints the driver
  line, per-group state/failure/holds/sessions, pending escalations;
  `_cmd_answer` (~2929) has `--action`, `--text`, `--text-file`; `_cmd_retry`
  (~2958). `run` acquires `DriverLock` around `scheduler.run()` (~2255).
- **Retry.** `orchestrator/execution/retry.py` — `_retry_failed` sets
  `entry.failure = None`; `_retry_quarantined` leaves the old text.
- **Observatory.** `orchestrator/observatory/runs.py` — `GroupHeartbeat`
  (pass-through pydantic model of `heartbeat.json`, ~111), `SnapshotSession`
  (`transcript_path`, `transcript_mtime`), `SnapshotGroup.heartbeat`,
  `_group_heartbeat`. `orchestrator/observatory/transcripts.py` parses a
  session transcript per request. UI: `ui/src/types.ts` `GroupHeartbeat`,
  `ui/src/components/GroupBoard.tsx` `phaseLine()` renders the phase line;
  tests in `ui/src/components/GroupBoard.test.tsx` (vitest, fixture
  `R20260726_GROUPING`). `npm run build` = `tsc && vite build`; `npm test` =
  `vitest run`. The UI suite is part of the preflight gate.
- **Transcript events.** `orchestrator/execution/transcript_events.py` —
  `parse_transcript(path)` → `NeutralEvent(role, timestamp, text, tool_name, tool_use_id, tool_input, tool_output, is_error)`; a tool call "has returned"
  iff a later event carries the same `tool_use_id` with `role == "tool"`.
- **Config.** `orchestrator/config.py` — one pydantic model per TOML section,
  registered on `OrchestratorConfig`; documented section by section in
  `docs/orchestrator-grouping-config.md`. There is no `[liveness]` section.
- **Live tier.** `pytest -m llm`; `tests/test_e2e_live.py` builds a
  session-scoped scratch repo and runs the `smart-mcps-orchestrate` CLI as a
  subprocess; `tests/test_streaming_live.py` drives a real `claude` child.
  `state.json` `live_pids` names the worker pid while a round runs.
- **Skill.** `skills/orchestrator-run/SKILL.md` Phase 2 carries the manual
  wedge check (phase comparison across two `status` calls, worktree and
  transcript mtime) and a "slow heartbeat (20–30 min)"; `triage-guide.md`
  `coder_question` section says the answer "lives only in that coder's
  session".
- **Glossary.** `CONTEXT.md` already defines Sign of Life, Liveness Window,
  Not Live, Operator Decision, Suspend Cure (uncommitted working-tree edit;
  commit it with this plan).
- **Version.** `.claude-plugin/plugin.json` is `0.17.2`; this plan ships as
  `0.18.0`.

## Decisions

- **The cure kills the child's pid tree, never a process group.** The child
  shares the driver's pgid (no `start_new_session`), so `killpg` would kill
  the driver. SIGTERM the child and every `/proc` descendant, grace, SIGKILL.
  Rejected: spawning children in their own session (Ctrl-C would no longer
  reach workers; new terminate-on-exit code on the SIGINT path). (→ ADR 0009)
- **The cure re-enters through the blocked call.** Killing the child makes
  the blocking worker call return; the runner raises a typed `SuspendCured`
  when the in-process registry marked that pid as cured; one wrapper in the
  review loop (`_worker_call`) around every worker call catches it and
  re-issues a warm resume of the same session id in the same generation
  (coder: the re-entry prompt; reviewer: the re-review prompt; a nudge:
  re-entry then nudge again). Covers coder and reviewer children alike.
  Rejected: coder-only (a hung reviewer is the same overnight case).
- **Child ↔ group matching is by worktree, not by session id.** The activity
  registry records the spawn `cwd`; a group's probe asks for the child whose
  cwd is its workspace. No review-loop change is needed to learn the
  reviewer's runner-generated session id. Rejected: pre-generating the
  reviewer id (extra manifest churn for a lookup the cwd already gives).
- **Launch phases end on the first `assistant` event.** The CLI's `system`
  init event arrives within a second and would make `starting the coder`
  meaningless; the first assistant turn still flips the r0913 g16 case. The
  liveness signal itself counts every event type (R1). Rejected: any event.
- **Suspend facts live on the run-scoped heartbeat file, written by the
  driver lock's thread.** `DriverLock` already runs for exactly the driver's
  lifetime; the monitor writes `<run>/heartbeat.json` (`last_wake_at`,
  `last_suspend_gap_s`, `suspends`) and group probes read it back from disk
  each tick — no new object threaded through `ReviewDeps`. Rejected: a
  monitor started from `_cmd_run` (another `cli.py` edit for no gain).
- **Facts only, with the window written next to them.** `heartbeat.json`
  carries `liveness_window_s`, `cures`, `max_cures_per_generation` so every
  reader (`status`, Observatory) derives Not Live and cures-exhausted from
  the file alone without loading config. No `not_live`/`stalled` key is ever
  written (the existing forbidden-key test is extended).
- **Binding is a flag on the answer record, not a new file.**
  `EscalationResponse.binding: bool = True`; the ledger is derived at
  prompt-render time from both escalation locations, filtered to
  `coder_question` requests answered with action `answer` and
  `binding = true`. `--guidance` on the CLI and `binding` on the Observatory
  POST body opt out; the panel gets no checkbox (every Observatory answer
  binds). Rejected: a per-group `decisions.md` (a second copy that drifts);
  an opt-in `--binding` (a forgotten flag repeats r0913).
- **Decisions are a prompt section, and the rewrite speccer gets them as
  skeleton input.** `## Operator decisions (binding)` is rendered verbatim
  into the fresh coder, handoff, reviewer, re-review and rewrite-speccer
  prompts; `spec` and `spec_sha256` are untouched, so resume forking is
  unaffected. The rewrite provider computes the ledger itself from `paths`
  (no `rewrite_spec` seam change).
- **Content filter escalates directly, no synthetic report.** `CoderReport`
  has a real shape; a `ContentFiltered` exception is caught by the same
  worker-call wrapper before any generic `SessionError` handler (including
  `_reenter`'s fallback), escalated as `CODER_BLOCKED` quoting the last
  assistant message, then `retry` → relaunch, `answer` → rewrite, no answer →
  rewrite with a context surprise naming the filter. Never warm-resumed.
- **All `cli.py` wiring lands in one unit.** `cli.py` alone is ~46k read
  tokens; four separate units touching it would each cost a near-cap group.
  Helpers live in `liveness.py`, `transcript_events.py`, `decisions.py`; one
  unit wires them into `status`, `answer` and `_rewrite_provider`.
- **The learning_podcast run is the acceptance step, not a unit.** A worker
  cannot drive a run in another repo; the run-driver skill does it after
  merge and records the three required observations in its notes file.

## Units

### U1. event-stamp — every stream event stamps the child's activity in an in-process registry

- **Summary**: [liveness] `StreamingProcess` gains an `on_event(type)` hook and captures the last assistant text; new `orchestrator/execution/liveness.py` ships an in-process `ActivityRegistry` (pid, session id, cwd, spawned-at, last event at/type, last assistant text, cured mark) that `SessionRunner._spawn` feeds, and `RoundHeartbeat` exposes `last_event_at` / `last_event_type` / `child_pid` / `child_spawned_at` from it.
- **Goal**: `StreamingProcess.__init__` accepts `on_event: Callable[[str], None] | None`; `_read_stdout` calls it with the event `type` for every parsed line (`assistant`, `user`, `system`, `result`, partial-message events) before any other handling, wrapped so a raising hook never breaks the reader. `StreamOutcome.last_assistant_text: str` holds the text blocks of the last `assistant` event, capped at 2,000 chars. `liveness.py` defines `ChildActivity` (dataclass: `pid`, `session_id`, `cwd`, `spawned_at` wall-clock ISO, `last_event_at`, `last_event_type`, `last_assistant_text`, `cured: bool`) and thread-safe `ActivityRegistry` with `spawned(pid, *, session_id, cwd)`, `note_event(pid, event_type)`, `exited(pid)`, `current(cwd) -> ChildActivity | None` (the live child whose cwd equals the given path, newest spawn wins), `mark_cured(pid)`, `was_cured(pid) -> bool`. `SessionRunner.__init__` takes `activity: ActivityRegistry | None = None`; `_spawn` registers the pid with the session id parsed from `context` and the call's `cwd`, wires `stream.on_event`, and reports exit; `_invoke` raises `SuspendCured(SessionError)` (new, carrying `session_id`) instead of the generic error when the exit was nonzero and `activity.was_cured(pid)`. `RoundHeartbeat.__init__` accepts `activity_provider: Callable[[], ChildActivity | None] | None`; `snapshot()` adds `last_event_at`, `last_event_type`, `child_pid`, `child_spawned_at` (all `None` when no child). The R7 notes in `streaming.py` and `sessions.py` module docstrings say explicitly that no round or work wall-clock limit exists and that liveness is a separate, evidence-only concern (R3).
- **Files**: `orchestrator/execution/streaming.py`, `orchestrator/execution/sessions.py`, `orchestrator/execution/heartbeat.py`, `orchestrator/execution/liveness.py` *(new, large)*, `tests/test_streaming.py`, `tests/test_liveness.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `ActivityRegistry`
- **Verification**:
  - Against `tests/fake_claude.py`, a round's `on_event` receives one call per stream line in order, including the `result` event, and a hook that raises does not prevent `wait()` from returning the envelope.
    Run: `uv run pytest tests/test_streaming.py -q -k on_event`
    Pass: green, and the assertion enumerates ≥ 3 distinct event types observed.
  - `ActivityRegistry.current(cwd)` returns the newest live child for that cwd, `None` after `exited`, and `note_event` for an unknown pid is a no-op (never raises).
    Run: `uv run pytest tests/test_liveness.py -q -k registry`
    Pass: green.
  - `SessionRunner` wired with a registry: after a fake-claude round, the registry saw `spawned` with the session id from `--session-id` and the call's cwd, then `exited`; a nonzero exit with `was_cured(pid)` true raises `SuspendCured` whose `session_id` is the argv's session id, and the same exit without the mark raises plain `SessionError`.
    Run: `uv run pytest tests/test_liveness.py -q -k runner`
    Pass: green.
  - Real-CLI oracle: against the installed `claude` binary (live tier), `on_event` observes a `system` event before the first `assistant` event and `last_assistant_text` is non-empty after `wait()`.
    Run: `uv run pytest tests/test_streaming_live.py -q -m llm -k on_event`
    Pass: green (skipped only when `claude` is not on PATH).
  - `heartbeat.json` written by a `RoundHeartbeat` with an `activity_provider` returning a fake `ChildActivity` carries `last_event_at`, `last_event_type`, `child_pid`, `child_spawned_at`; with no provider all four are `null`; no forbidden key appears.
    Run: `uv run pytest tests/test_heartbeat.py -q`
    Pass: green.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U2. sign-of-life — three signals evaluated every heartbeat tick under a configurable Liveness Window, with honest launch phases

- **Summary**: [liveness] `[liveness]` config (`window_seconds=600`, `suspend_gap_seconds=60`, `kill_grace_seconds=10`, `max_cures_per_generation=2`) and a `LivenessProbe` hung on the heartbeat tick that reads `/proc` for the group's child, records `last_sign_of_life_at` / signal / one-line evidence / `liveness_window_s` into `heartbeat.json` as facts only, and flips `starting the coder`-class phases to `round <r> running` on the first assistant event.
- **Goal**: `LivenessConfig` pydantic model in `config.py`, registered as `OrchestratorConfig.liveness`, documented as a `[liveness]` section in `docs/orchestrator-grouping-config.md`. `liveness.py` gains pure helpers over a `proc_root: Path` parameter (default `/proc`): `cpu_ticks(pid)` (utime+stime, fields 14+15 of `stat`, split after `)`), `children_of(pid)` (pids whose `ppid` is `pid`, state not `Z`, with `cmdline` head), `descendants(pid)`, and `sign_of_life(child, *, prev_cpu, now, window_s, proc_root) -> SignOfLife(at, signal, evidence, cpu_ticks)` evaluating (a) `last_event_at` within the window, (b) a live non-zombie child process whose parent is the pid, (c) CPU ticks advanced since `prev_cpu`. Read errors (`ENOENT`, a pid gone between listing and reading, unparsable `stat`) count as "no signal from that source". `LivenessProbe(heartbeat, config, activity_provider, log)` is installed as the heartbeat's `on_tick` (composing with, not replacing, the existing transcript probe: the heartbeat now holds a list of tick hooks); each tick it evaluates the three signals for the current child, remembers the previous CPU ticks per pid, and updates the heartbeat facts `last_sign_of_life_at`, `sign_of_life_signal` (`event` | `tool_child` | `cpu` | `null`), `sign_of_life_evidence` (e.g. `event assistant 12s ago; tool child "uv run pytest tests/…" 4m10s; cpu advancing`), `liveness_window_s`, `cures` (0 until U5 fills it), `max_cures_per_generation`. Not Live is *derived* by readers as `now − max(last_sign_of_life_at, child_spawned_at) > liveness_window_s` while a child exists; with no child (between rounds, merging) nothing is Not Live. Launch phases: when the current phase is one of `starting the coder`, `forking the base session`, `resuming the interrupted coder`, `reviewer verifying the report` (the reviewer's launch) and the first `assistant` event arrives for the current child, the probe calls `mark_phase(f"round {round} running")` (reviewer: `f"round {round} review running"`); the phase set before the event is left untouched. `RoundHeartbeat.snapshot()` writes all new keys; the forbidden-key test is extended with `not_live`, `live`, `wedged`, `dead`.
- **Files**: `orchestrator/execution/liveness.py`, `orchestrator/execution/heartbeat.py`, `orchestrator/config.py`, `docs/orchestrator-grouping-config.md`, `tests/test_liveness.py`, `tests/test_heartbeat.py`
- **Symbols**: —
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: implements `heartbeat.liveness-facts`; consumes `ActivityRegistry`
- **Verification**:
  - Against a fake `/proc` tree under `tmp_path` (a `stat` with a parenthesised comm containing a space, one zombie child, one live child with a `cmdline`): signal (b) fires only for the live non-zombie child whose `ppid` matches; signal (c) fires when ticks advance and not when flat; a pid whose directory vanishes between listing and reading yields "no signal", never an exception.
    Run: `uv run pytest tests/test_liveness.py -q -k sign_of_life`
    Pass: green.
  - `load_config` on a TOML with `[liveness] window_seconds = 20` yields `config.liveness.window_seconds == 20`; the defaults are 600 / 60 / 10 / 2 with no file.
    Run: `uv run pytest tests/test_liveness.py -q -k config`
    Pass: green.
  - A `RoundHeartbeat` with an installed probe and a provider returning a child whose `last_event_at` is fresh writes `sign_of_life_signal == "event"`; with a stale event but a live fake tool child it writes `tool_child` and the evidence names the child's cmdline head; with both stale and flat CPU it writes `null` and the previous `last_sign_of_life_at` is preserved unchanged.
    Run: `uv run pytest tests/test_heartbeat.py -q -k sign_of_life`
    Pass: green.
  - Phase flip: heartbeat in phase `starting the coder` at round 1; the probe sees the child's `last_event_type == "system"` → phase unchanged; then `assistant` → `heartbeat.json` phase reads `round 1 running`; the existing transcript-probe hook still runs on the same tick.
    Run: `uv run pytest tests/test_heartbeat.py -q -k phase_flip`
    Pass: green.
  - Real `/proc` oracle: `sign_of_life` evaluated on a real `subprocess.Popen(["sleep", "30"])` spawned by a real `sh -c` parent reports signal `tool_child` for the parent pid with the evidence naming `sleep`; a `SIGSTOP`ped busy child (`python -c "while True: pass"`) reports flat CPU after two samples and advancing CPU after `SIGCONT`.
    Run: `uv run pytest tests/test_liveness.py -q -k real_proc`
    Pass: green on Linux.
  - `docs/orchestrator-grouping-config.md` has a `## [liveness]` section listing the four fields, their defaults and effects, and states that no field limits a round's or a group's wall-clock duration.
    Run: `grep -n "^## .*\[liveness\]" docs/orchestrator-grouping-config.md && grep -c "window_seconds\|suspend_gap_seconds\|kill_grace_seconds\|max_cures_per_generation" docs/orchestrator-grouping-config.md`
    Pass: the heading is found and the count is ≥ 4.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U3. not-live-report — Not Live enters and leaves the run log once, the driver line counts live groups, and one helper renders the `status` liveness line

- **Summary**: [liveness] The probe writes one run-log Lifecycle Event on entering Not Live (`group <gid> generation <n>: not live for <age> in <phase> — <evidence>`) and one on leaving (`… live again: <signal> <age> ago`), never per tick; `driver_status_line` stops saying `progressing` from the heartbeat mtime and reports how many active groups are live and how many are not; `liveness.py` ships `liveness_line(heartbeat: dict, *, now) -> str` and `cures_exhausted_line(heartbeat, run_id, driver_pid) -> str | None` for `status` (wired in U12) and the same derivation rule the Observatory applies.
- **Goal**: `LivenessProbe` keeps an in-memory `_not_live_since` (never persisted) and logs the two transitions through the heartbeat's `log` sink; a child that exits while Not Live logs the leaving line with `child exited`. `driver.py` gains `group_liveness(paths, group_ids, *, now) -> tuple[int, int]` (live, not-live counts derived from each group's `heartbeat.json` facts with the rule from U2) and `driver_status_line` renders `a process is driving this run (pid N): 2 active groups live` or `…: 1 live, 1 NOT LIVE (see the group lines)`; `STALE_HEARTBEAT_SECONDS` remains only for the "heartbeat file itself is stale" case. `liveness_line` renders `live: <signal> <age> ago` for a live child, `NOT LIVE for <age> in <phase> — <evidence>` for a Not Live one, `no worker child (<phase>)` when `child_pid` is null, and `(no liveness facts)` for a heartbeat written before this shipped. `cures_exhausted_line` returns `cures exhausted (<n>/<max> this generation) — kill -INT -<pgid> then smart-mcps-orchestrate resume <run_id>` when `cures >= max_cures_per_generation`, with `<pgid>` from `os.getpgid(driver_pid)`, else `None`.
- **Files**: `orchestrator/execution/liveness.py`, `orchestrator/execution/driver.py`, `tests/test_liveness.py`, `tests/test_driver_liveness.py`
- **Symbols**: —
- **Depends-on**: U2
- **Slice**: —
- **Implements / Consumes**: implements `liveness-line`; consumes `heartbeat.liveness-facts`
- **Verification**:
  - Driving a probe with a fake clock through 40 ticks where the child goes silent at tick 5 (window 60 s, tick 15 s): exactly one `not live for` line is logged (at the first tick past the window) and exactly one `live again` line after an event returns at tick 30; the tick count between them logs nothing.
    Run: `uv run pytest tests/test_liveness.py -q -k transitions`
    Pass: green, with the two line texts asserted verbatim against the R4 format.
  - `driver_status_line` over two written `heartbeat.json` files (one fresh Sign of Life, one 20 minutes stale with a child pid) reads `1 live, 1 NOT LIVE`; with no child pids in either file it reads `2 active groups, no worker child`; the word `progressing` no longer appears anywhere in `driver.py`.
    Run: `uv run pytest tests/test_driver_liveness.py -q -k liveness && ! grep -n "progressing" orchestrator/execution/driver.py`
    Pass: tests green and the grep finds nothing.
  - `liveness_line` on the four heartbeat shapes (live, not live, no child, pre-liveness file) returns the four documented strings; `cures_exhausted_line` returns `None` below the cap and the exact command line at the cap.
    Run: `uv run pytest tests/test_liveness.py -q -k liveness_line`
    Pass: green.
  - Real-run oracle: `liveness_line` applied to every `groups/*/heartbeat.json` under `.orchestrator/runs/` of this repo returns `(no liveness facts)` for each (they predate the field) and never raises.
    Run: `uv run python -c "import json,pathlib,time; from orchestrator.execution.liveness import liveness_line; [print(p, liveness_line(json.loads(p.read_text()), now=time.time())) for p in pathlib.Path('.orchestrator/runs').glob('*/groups/*/heartbeat.json')]"`
    Pass: every printed line ends with `(no liveness facts)` and the command exits 0.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U4. suspend-detect — a machine suspend is a recorded fact on the run-scoped heartbeat, sampled by the driver lock's thread

- **Summary**: [liveness] `SuspendMonitor` samples `CLOCK_MONOTONIC` vs `CLOCK_BOOTTIME` on every driver-record tick (fallback: a wall-clock gap larger than five intervals), logs `machine suspend detected: <gap>` once per suspend, and stamps `last_wake_at` / `last_suspend_gap_s` / `suspends` on `<run>/heartbeat.json`; `DriverLock` starts and stops it, and `read_suspend_facts(paths)` gives group probes the wake time.
- **Goal**: `liveness.py` `SuspendMonitor(paths, *, gap_s, interval_s, log, clock=time.monotonic, boottime=lambda: time.clock_gettime(time.CLOCK_BOOTTIME), wall=time.time)` with a pure `sample()` method returning a detected gap or `None`: boot-time delta minus monotonic delta > `gap_s` → suspend of that gap; else wall delta > 5 × `interval_s` → suspend of the wall delta. On detection it merges `{"schema_version", "last_wake_at" (wall ISO), "last_suspend_gap_s", "suspends" (count), "updated_at"}` into the run-scoped heartbeat file via `atomic_write_text` (keeping any keys already there) and logs one run-log line. `DriverLock.acquire()` constructs it with `gap_s = config.liveness.suspend_gap_seconds` (passed in by the caller in `cli.py` through a new `DriverLock(paths, *, suspend_gap_s=…)` kwarg with default 60 so existing callers and tests need no change) and calls `sample()` from the existing record thread; `release()` stops it. `read_suspend_facts(paths) -> SuspendFacts | None` reads the file tolerantly. Every write is best-effort like the heartbeat.
- **Files**: `orchestrator/execution/liveness.py`, `orchestrator/execution/driver.py`, `tests/test_liveness.py`, `tests/test_driver_liveness.py`
- **Symbols**: —
- **Depends-on**: U2
- **Slice**: —
- **Implements / Consumes**: implements `suspend-facts`
- **Verification**:
  - With injected clocks: monotonic advances 10 s while boot time advances 400 s → `sample()` reports a 390 s suspend; both frozen while wall advances 200 s at a 10 s interval → reports 200 s (WSL2 path); monotonic and boot time advancing together → `None`; the file after a detection carries `last_wake_at` and `suspends == 1`, and a second detection makes `suspends == 2`.
    Run: `uv run pytest tests/test_liveness.py -q -k suspend`
    Pass: green.
  - `DriverLock` acquired with `record_interval=0.05` on a `tmp_path` run writes the driver record as before **and** `read_suspend_facts` returns `None` until a monitor sample fires; forcing the monitor's clocks to diverge makes the facts appear within one interval; `release()` joins the thread.
    Run: `uv run pytest tests/test_driver_liveness.py -q -k suspend`
    Pass: green.
  - Real-clock oracle: on this host, `time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()` sampled twice one second apart differs by < 0.5 s, so the default monitor never reports a suspend on an awake machine (documented as the false-positive guard).
    Run: `uv run pytest tests/test_liveness.py -q -k awake_host`
    Pass: green.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U5. suspend-cure — after a detected suspend, a child with no Sign of Life since the wake is killed by pid tree and warm-resumed in place, counted and capped per generation

- **Summary**: [liveness] The probe cures (SIGTERM the child + `/proc` descendants, `kill_grace_seconds`, SIGKILL, mark cured) only when `last_wake_at` is set and the child has had no Sign of Life since it for a whole window and `cures < max_cures_per_generation`; the review loop routes every worker call through `_worker_call`, which catches `SuspendCured` and warm-resumes the same session id in the same generation; `GroupRunState.cures` (per generation) is persisted through a new `GroupContext.record_cure`, never touching `reentry_count`.
- **Goal**: `liveness.py` `kill_tree(pid, *, grace_s, proc_root, kill=os.kill)`: SIGTERM to `pid` and `descendants(pid)`, wait up to `grace_s` for each to disappear, SIGKILL the rest; returns the pids signalled. `LivenessProbe` gets `suspend_facts_provider` (reads `read_suspend_facts(paths)`), `cures_provider() -> int` and `on_cure(pid, generation, session_id)`; the cure condition is `last_wake_at is not None and max(last_sign_of_life_at, child_spawned_at) < last_wake_at and now − last_wake_at > liveness_window_s and cures < max`; on cure it logs `group <gid> generation <n>: suspend cure <k>/<max> — no sign of life since wake at <t>; killing session <sid> pid <pid>`, calls `activity.mark_cured(pid)`, `kill_tree`, and `on_cure`. Past the cap it logs once `… cures exhausted (<max>/<max>); reporting only` and never kills again in that generation. `scheduler.py`: `GroupRunState.cures: dict[str, int]` keyed by generation as a string (JSON-safe), `GroupContext.record_cure: Callable[[], int]` increments the current generation's count, persists `state.json`, returns the new count; `reentry_count` is untouched by a cure. `review.py`: `_worker_call(fn, *, session_id, role, **kwargs)` wraps every `asyncio.to_thread(runner.…)` site (first launch, revision resume, reviewer launch, re-review, extra pass, answer resume, re-entry resume, and the `nudge_until_report` call); on `SuspendCured` it logs `group <gid> generation <n>: warm-resuming session <sid> after suspend cure` and re-issues `runner.resume(session_id, prompt=render_reentry_prompt(group) | render_re_review_prompt(report_path), cwd, on_turn)` through the same wrapper (so a second cure re-issues again until the cap); the round then continues with the resumed result. The heartbeat's probe is given the loop's `cures_provider` (`entry.cures.get(str(generation), 0)`) and `on_cure` (→ `ctx.record_cure()`). A `SuspendCured` raised from a first-launch `start_worker` whose session never registered (no transcript yet) falls through to the existing `SessionError` path (documented in the wrapper's docstring). The `_reenter` fallback `except SessionError` is placed after the wrapper so a cure never forks a fresh generation.
- **Files**: `orchestrator/execution/liveness.py`, `orchestrator/execution/review.py`, `orchestrator/execution/scheduler.py`, `tests/test_liveness.py`, `tests/test_suspend_cure.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U2, U4
- **Slice**: —
- **Implements / Consumes**: consumes `suspend-facts`, `ActivityRegistry`
- **Verification**:
  - Cure predicate table (fake clock, fake facts): no `last_wake_at` + Not Live for 2 windows → no cure (R7's last sentence); `last_wake_at` set + Sign of Life after it → no cure; `last_wake_at` set + no Sign of Life since + one window elapsed → cure; cures already at the cap → no kill and one `cures exhausted` line.
    Run: `uv run pytest tests/test_liveness.py -q -k cure_predicate`
    Pass: green; the four cases are asserted separately.
  - `kill_tree` on a real process tree (`sh -c "sleep 60 & sleep 60; wait"`) signals the shell and both `sleep`s; after the call none of the three pids exists; a pid that already exited is skipped without raising.
    Run: `uv run pytest tests/test_liveness.py -q -k kill_tree`
    Pass: green.
  - Review-loop scenario with the scripted runner (`tests/test_review_loop.py`'s `Harness` pattern): the runner raises `SuspendCured(session_id="c1")` on the first `resume` call and returns a normal round on the second; the group completes in generation 1, `state.json` shows `cures == {"1": 1}`, `reentry_count == 0`, the manifest has exactly one coder session, and the run log carries the `warm-resuming session c1 after suspend cure` line.
    Run: `uv run pytest tests/test_suspend_cure.py -q -k warm_resume`
    Pass: green.
  - Same scenario for a reviewer round: `SuspendCured` on the reviewer's first call → re-issued with the re-review prompt; the verdict is recorded and the generation is unchanged.
    Run: `uv run pytest tests/test_suspend_cure.py -q -k reviewer`
    Pass: green.
  - A `ContentFiltered`/generic `SessionError` from the same sites still reaches its existing handler (the wrapper is transparent to everything but `SuspendCured`).
    Run: `uv run pytest tests/test_review_loop.py -q`
    Pass: the whole existing file stays green.
  - Real-kernel oracle: `_is_same_process`/`kill_tree` interplay — after `kill_tree` on a real `claude`-shaped fake (`tests/fake_claude.py` spawned via `StreamingProcess`), `StreamingProcess.wait()` returns a nonzero `returncode` within `kill_grace_seconds + 2` and the registry reports the pid as cured.
    Run: `uv run pytest tests/test_liveness.py -q -k cure_returns`
    Pass: green.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U6. activity-tail — the last five worker actions read from a session transcript, as a helper for `status` and the Observatory

- **Summary**: [visibility] `transcript_events.activity_tail(path, *, n=5) -> list[ActivityEntry]` returns the last `n` tool calls of a Claude Code transcript (time, tool name, first line of the tool input, returned yes/no) without persisting anything; `format_activity_tail(entries)` renders the `status` lines.
- **Goal**: `ActivityEntry` pydantic model (`at: str | None`, `tool: str`, `input_head: str` — first line of the JSON-ish input, capped at 120 chars, `returned: bool`) built from `parse_transcript` events: each `tool_use` event is an entry; `returned` is true iff a later `role == "tool"` event carries the same `tool_use_id`. Reads the file tolerantly (missing → `[]`, torn tail line skipped). `format_activity_tail` produces `  <HH:MM:SS> <tool> <input_head> [returned|running]` lines, or `  (no tool calls yet)`. Both are read-only and stateless.
- **Files**: `orchestrator/execution/transcript_events.py`, `tests/test_transcript_events.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `activity-tail`
- **Verification**:
  - On a synthetic transcript with seven tool calls where the last has no result, `activity_tail(n=5)` returns five entries, the last with `returned == False`, ordered oldest→newest, `input_head` truncated at 120 chars with an ellipsis, and a `Bash` call's head is the command's first line.
    Run: `uv run pytest tests/test_transcript_events.py -q -k activity_tail`
    Pass: green.
  - Real-transcript oracle: over the newest real `*.jsonl` under `~/.claude/projects/` with ≥ 20 `tool_use` blocks, `activity_tail` returns exactly 5 entries, every `tool` is a non-empty string, and at most one entry has `returned == False`.
    Run: `uv run pytest tests/test_transcript_events.py -q -k real_transcript_tail`
    Pass: green (skipped only when no such transcript exists).
- **Edge cases**: —
- **Non-goals / must-not**: —

### U7. observatory-liveness — the Observatory shows the liveness line and the activity tail from `heartbeat.json` and the transcript it already reads

- **Summary**: [visibility] `GroupHeartbeat` passes the new liveness facts through, `SnapshotSession.activity_tail` carries the last five actions of each session with a transcript, and the board card renders a liveness line (derived client-side by the same rule as `status`) plus the tail of the group's newest session; the Observatory never acts on liveness.
- **Goal**: `runs.py`: `GroupHeartbeat` gains optional `last_event_at`, `last_event_type`, `child_pid`, `child_spawned_at`, `last_sign_of_life_at`, `sign_of_life_signal`, `sign_of_life_evidence`, `liveness_window_s`, `cures`, `max_cures_per_generation` (all `None` when absent); `SnapshotSession.activity_tail: list[ActivityEntry]` filled through `transcript_events.activity_tail` (empty when no transcript). `ui/src/types.ts` mirrors both. `GroupBoard.tsx` adds `livenessLine(group)` (same four shapes as U3's helper, computed from `updated_at`/`last_sign_of_life_at`/`child_spawned_at`/`liveness_window_s`; `NOT LIVE` carries a `group-card__liveness--not-live` class) and an `activity tail` list of the newest session's entries. `docs/observatory.md` documents the new snapshot fields and that they are facts only.
- **Files**: `orchestrator/observatory/runs.py`, `ui/src/types.ts`, `ui/src/components/GroupBoard.tsx`, `ui/src/components/GroupBoard.test.tsx`, `docs/observatory.md`, `tests/test_observatory_api.py`
- **Symbols**: —
- **Depends-on**: U2, U6
- **Slice**: —
- **Implements / Consumes**: consumes `heartbeat.liveness-facts`, `activity-tail`
- **Verification**:
  - Snapshot API over a fixture run whose `heartbeat.json` carries liveness facts and whose session transcript has tool calls returns the facts verbatim and a five-entry `activity_tail`; a run predating the fields returns `null`s and `[]` with status 200.
    Run: `uv run pytest tests/test_observatory_api.py -q -k "liveness or activity_tail"`
    Pass: green.
  - Board test: a card whose heartbeat is 20 minutes past its window renders `NOT LIVE for` with the evidence text and the not-live class; a fresh one renders `live:`; a pre-liveness fixture group renders neither and no error.
    Run: `cd ui && npm test -- GroupBoard`
    Pass: green.
  - Real build oracle: the UI type-checks and builds.
    Run: `cd ui && npm run build`
    Pass: exit 0.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U8. decision-ledger — binding answers are a flag on the response record and a ledger derived from the escalation files

- **Summary**: [decisions] `EscalationResponse.binding: bool = True`, `answer_escalation(..., binding=True)`, the Observatory POST body's `binding` field, and new `orchestrator/execution/decisions.py` computing `decision_ledger(paths, group_id)` from both escalation locations (answered `coder_question` requests with action `answer` and `binding` true, ordered by time) and rendering `## Operator decisions (binding)` verbatim; the bundle export passes the flag through.
- **Goal**: `OperatorDecision(escalation_id, at, generation, question, answer)`; `decision_ledger` globs `request-*.json` under `<run>/escalations/` and `<run>/groups/<gid>/`, matches responses by id, keeps `kind == coder_question`, `action == answer`, `binding == true`, sorts by `answered_at`; `render_decisions_section(ledger) -> str` returns `""` for an empty ledger, else the section header followed by one block per entry: `- [<escalation_id> @ <answered_at>] Question: <question verbatim>` then `  Decision: <answer verbatim>` (multi-line answers indented, never trimmed or reflowed). `answer_escalation` gains `binding: bool = True` and writes it; `post_answer`'s body model gains `binding: bool = True`. `ExportEscalation.binding: bool | None` is added and `docs/run-bundle-contract.md` lists it (additive, schema version unchanged). Older response files without the field load as `binding = True`.
- **Files**: `orchestrator/execution/decisions.py` *(new, medium)*, `orchestrator/model.py`, `orchestrator/execution/escalation.py`, `orchestrator/observatory/escalations.py`, `orchestrator/execution/export.py`, `docs/run-bundle-contract.md`, `tests/test_decisions.py` *(new, medium)*, `tests/test_escalation.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `decision-ledger`
- **Verification**:
  - A run dir with three answered `coder_question` requests (one `binding: false`, one under `groups/g1/`, one under `escalations/`), one answered `coder_blocked`, one `coder_question` answered with `retry`, and one unanswered: the ledger for `g1` has exactly the two binding `answer`ed questions in `answered_at` order; the rendered section quotes both answers byte-for-byte including a backtick and a `$`.
    Run: `uv run pytest tests/test_decisions.py -q`
    Pass: green.
  - `answer_escalation(..., binding=False)` writes `"binding": false`; the default writes `true`; a response file lacking the key validates with `binding == True`.
    Run: `uv run pytest tests/test_escalation.py -q -k binding`
    Pass: green.
  - Observatory `POST …/escalations/<id>/answer` with `{"action": "answer", "text": "x", "binding": false}` writes a non-binding record; the body without `binding` writes a binding one.
    Run: `uv run pytest tests/test_observatory_escalations.py -q -k binding`
    Pass: green.
  - Real-run oracle: `decision_ledger` over every group of every run under this repo's `.orchestrator/runs/` (all predate the flag) never raises and returns only entries whose request files really have `kind == "coder_question"`.
    Run: `uv run python -c "import json; from pathlib import Path; from orchestrator.execution.manifest import RunPaths; from orchestrator.execution.decisions import decision_ledger; [print(r.name, g.name, len(decision_ledger(RunPaths(Path('.'), r.name), g.name))) for r in Path('.orchestrator/runs').iterdir() for g in (r/'groups').glob('g*') if (r/'groups').is_dir()]"`
    Pass: exit 0 and a line per group.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U9. decisions-section — every later coder, handoff, reviewer and re-review prompt of the group carries the ledger, and the reviewer treats a decision as a spec amendment

- **Summary**: [decisions] `render_coder_prompt`, `render_handoff_prompt`, `render_reviewer_prompt` and `render_re_review_prompt` take `decisions: str` and place it verbatim; the review loop computes the ledger from disk at every render; `reviewer.md` states the amendment rule (work following a decision is never self-invented; work contradicting one is `changes_required` citing the escalation id), `coder.md`/`handoff.md` say the section is binding, and `rewrite_speccer.md` honours an `operator_decisions` skeleton key.
- **Goal**: each of the four renderers gains a keyword `decisions: str = ""` substituted into a new `$decisions` slot placed after the identity/spec block and before the task-specific text; `review.py` adds `_decisions_text()` → `render_decisions_section(decision_ledger(deps.store.paths, gid))` and passes it at every call site (fresh coder prompt in `_run_generation`, `_prepare_handoff`, both reviewer prompts in `_review_round`). `reviewer.md` gains a paragraph: a listed operator decision amends the spec for this group; do not judge work that follows it as self-invented or out of scope; work that contradicts it is `changes_required` with the required change citing the decision's escalation id. `coder.md` and `handoff.md` gain one line: the decisions section, when present, is binding and overrides the spec where they differ. `rewrite_speccer.md` gains: `operator_decisions` in GROUPS_JSON are binding human decisions; the rewritten spec must incorporate them verbatim and must not contradict them. `spec` and `spec_sha256` are never modified by any of this.
- **Files**: `orchestrator/execution/prompting.py`, `orchestrator/execution/review.py`, `orchestrator/prompts/coder.md`, `orchestrator/prompts/handoff.md`, `orchestrator/prompts/reviewer.md`, `orchestrator/prompts/re_review.md`, `orchestrator/prompts/rewrite_speccer.md`, `tests/test_decisions_carry.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U8
- **Slice**: —
- **Implements / Consumes**: consumes `decision-ledger`
- **Verification**:
  - Each of the four renderers with `decisions=""` produces no `Operator decisions` text and is byte-identical to today's output for the same inputs; with a two-entry section every prompt contains it verbatim exactly once.
    Run: `uv run pytest tests/test_decisions_carry.py -q -k render`
    Pass: green.
  - Review-loop scenario (scripted runner): generation 1's coder asks a question, the broker answers `four chapters, not six` (binding), the coder retires on the breaker; the generation-2 handoff prompt and the generation-2 reviewer's first prompt both contain `four chapters, not six` under `## Operator decisions (binding)` with the escalation id; the manifest's `spec_sha256` for both generations is identical.
    Run: `uv run pytest tests/test_decisions_carry.py -q -k carries`
    Pass: green.
  - The same scenario with the answer marked `binding: false` puts nothing in either prompt.
    Run: `uv run pytest tests/test_decisions_carry.py -q -k guidance_only`
    Pass: green.
  - Real-template oracle: the shipped `reviewer.md` contains the words `changes_required` and `escalation id` in the amendment paragraph, and `rewrite_speccer.md` names `operator_decisions`.
    Run: `grep -c "escalation id" orchestrator/prompts/reviewer.md && grep -c "operator_decisions" orchestrator/prompts/rewrite_speccer.md`
    Pass: both counts ≥ 1.
  - Real-fixture substitution oracle: all four renderers, run against every `Group` loaded from the tracked `tests/fixtures/observatory/run-modern/groups.json` with a two-entry ledger whose answers contain `$`, backticks and a `{`, substitute cleanly through the shipped templates (no `KeyError`/`ValueError` from `string.Template`), leave no `$identifier` placeholder in the output, and contain each answer byte-for-byte.
    Run: `uv run pytest tests/test_decisions_carry.py -q -k fixture_groups`
    Pass: green.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U10. content-filter-kind — a content-filter error is its own failure kind, never warm-resumed, routed like a blocked coder with the last assistant message quoted

- **Summary**: [failure-routes] `ContentFiltered(SessionError)` is raised by `_invoke` when the error text matches the content-filter phrase, carrying `last_assistant_text`; the review loop's `_worker_call` catches it before any generic `SessionError` handler and escalates `CODER_BLOCKED` quoting that text, then `retry` → relaunch, `answer` → rewrite, no answer → rewrite with a context surprise naming the filter.
- **Goal**: `sessions.py`: `_CONTENT_FILTER_RE = re.compile(r"content filter(?:ing)? policy|blocked by content filter", re.I)`; `ContentFiltered(SessionError)` with `.last_assistant_text`; raised from both the `is_error` envelope branch and the nonzero-exit branch when `detail` matches; `last_assistant_text` comes from `StreamOutcome.last_assistant_text` (U1) threaded through `_spawn`'s return. `review.py`: `_worker_call` catches `ContentFiltered` and raises a loop-internal `_ContentFilterStop(exc)`; `_run_generation` catches it and calls `_on_content_filtered(exc)`: log `group <gid> generation <n> round <r>: ended (content filter)`, escalate `EscalationKind.CODER_BLOCKED` with prompt `coder for <gid> hit the API content filter — last assistant message: "<text, capped 1,000 chars>"` and `want_diff=True`; `_is_retry` → `_relaunch`; otherwise `_rewrite("content filter", extra=[_context_surprise(gid, "API content filter blocked the coder's output; last message: …"), operator surprise if answered])`. `_reenter` never sees it as a fallback case (the wrapper converts it first). A reviewer `ContentFiltered` takes the same route.
- **Files**: `orchestrator/execution/sessions.py`, `orchestrator/execution/review.py`, `tests/test_content_filter.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U1, U5
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `_invoke` on a fake-claude envelope `{"is_error": true, "result": "API Error: Output blocked by content filtering policy"}` raises `ContentFiltered` with `last_assistant_text` equal to the last assistant text the fake emitted; the same envelope text on a nonzero exit also raises it; `usage limit reached` still raises `UsageLimit`.
    Run: `uv run pytest tests/test_content_filter.py -q -k classify`
    Pass: green.
  - Review-loop scenario HITL on: the first coder round raises `ContentFiltered`; exactly one `coder_blocked` escalation is raised whose prompt quotes the last assistant text; no `resume` call is made on that session id; answering `retry` relaunches on the same spec; answering `answer` performs one rewrite whose surprises include the operator text.
    Run: `uv run pytest tests/test_content_filter.py -q -k hitl`
    Pass: green.
  - HITL off: the same failure performs one rewrite whose surprises name the content filter and quote the message; the group is never `INTERRUPTED`.
    Run: `uv run pytest tests/test_content_filter.py -q -k autonomous`
    Pass: green.
  - Real-run oracle: the recorded envelope text from learning_podcast run r20260907-123644 g1 (`API Error: Output blocked by content filtering policy`, copied verbatim into the test as a constant with the run id in a comment) matches `_CONTENT_FILTER_RE`, and the existing `tests/test_sessions.py` usage-limit strings do not.
    Run: `uv run pytest tests/test_content_filter.py -q -k phrase && uv run pytest tests/test_sessions.py -q`
    Pass: both green.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U11. retry-release-note — `retry` leaves a release note instead of a stale failure line

- **Summary**: [failure-routes] Both `retry` paths set the group's `failure` to `released by operator at <ISO time>; resume to continue`, so `status` shows the release until the group re-enters and the scheduler's existing `set_state(RUNNING)` clears it.
- **Goal**: `_retry_failed` and `_retry_quarantined` in `retry.py` write the release text (UTC, seconds precision) instead of `None` / the old text; the run-log line is unchanged.
- **Files**: `orchestrator/execution/retry.py`, `tests/test_retry.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - After `retry_group` on a FAILED group and on a quarantined one, `state.json` has `failure` starting with `released by operator at ` and ending with `; resume to continue`; a scheduler `set_state(gid, RUNNING)` afterwards clears it.
    Run: `uv run pytest tests/test_retry.py -q -k release_note`
    Pass: green.
  - Real-CLI oracle: `smart-mcps-orchestrate retry <run> <gid>` on a fixture run under `tmp_path` with a FAILED group and a real git worktree, then `smart-mcps-orchestrate status <run>`, prints a `failure: released by operator at` line.
    Run: `uv run pytest tests/test_retry.py -q -k status_shows_release`
    Pass: green.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U12. cli-surfaces — `status` prints liveness, cures-exhausted and the activity tail; `answer --guidance` opts out of binding; the rewrite speccer receives the ledger

- **Summary**: [wiring] The one `cli.py` unit: `_cmd_status` prints per active group the U3 liveness line, the cures-exhausted line when due, and the U6 activity tail of the group's newest session; `answer` gains `--guidance` (`binding=False`, valid only with `--action answer`); `_rewrite_provider` takes `paths` and adds `operator_decisions` (the U8 ledger, verbatim) to the skeleton.
- **Goal**: `_cmd_status`: for every group in `RUNNING` / `REVIEWING` / `MERGING`, after the session lines, print ` liveness:` + `liveness_line(heartbeat, now)`, then `cures_exhausted_line(...)` if not `None` (driver pid from `read_driver_record`), then `  activity:` followed by `format_activity_tail(activity_tail(newest session transcript))`; groups without a heartbeat print nothing extra. `answer` parser: `--guidance` store-true; `_cmd_answer` passes `binding=not args.guidance` and rejects `--guidance` with any action other than `answer` (exit 2 with a one-line reason). `_rewrite_provider(plan_text, llm_runner, failure_dir, recorder, paths)`: skeleton gains `"operator_decisions": [f"[{d.escalation_id} @ {d.at}] Q: {d.question}\nA: {d.answer}" …]`; the `subject` record gains `"operator_decisions": <count>`.
- **Files**: `orchestrator/cli.py`, `tests/test_cli_liveness_and_decisions.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U3, U6, U8
- **Slice**: —
- **Implements / Consumes**: consumes `liveness-line`, `activity-tail`, `decision-ledger`
- **Verification**:
  - `status` on a fixture run with one RUNNING group whose `heartbeat.json` is 20 minutes past its window and whose session transcript has tool calls prints a `liveness: NOT LIVE for` line, no cures line (cures 0), and five `activity:` entries; with `cures == max` it prints the `cures exhausted` line containing `kill -INT -` and `resume <run_id>`.
    Run: `uv run pytest tests/test_cli_liveness_and_decisions.py -q -k status`
    Pass: green.
  - `answer <run> <id> --guidance --text x` writes `binding: false`; without the flag `binding: true`; `--guidance --action retry` exits 2 and writes no response file.
    Run: `uv run pytest tests/test_cli_liveness_and_decisions.py -q -k guidance`
    Pass: green.
  - The rewrite provider's recorded call (`llm/calls.json` via `JsonlCallRecorder` with a stub runner) shows a skeleton whose `operator_decisions` contains the fixture answer verbatim and a `subject.operator_decisions == 1`.
    Run: `uv run pytest tests/test_cli_liveness_and_decisions.py -q -k rewrite_provider`
    Pass: green.
  - Real-CLI oracle: `uv run smart-mcps-orchestrate status` against this repo's newest real run exits 0 and prints no traceback (every group predates the liveness facts).
    Run: `uv run smart-mcps-orchestrate status`
    Pass: exit 0.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U13. skill-cadence — the run-driver skill watches events, not heartbeats, and answers questions as binding decisions

- **Summary**: [docs] `SKILL.md` Phase 2 drops the manual wedge check and the 20–30 minute heartbeat; Monitor conditions add the `not live for`, `live again`, `machine suspend detected`, `suspend cure` and `cures exhausted` lines and the fallback `status` runs at most hourly; the manual `kill -INT -<pgid>` + `resume` is reserved for a group `status` reports Not Live with cures exhausted or no suspend detected; `triage-guide.md`'s `coder_question` section says `answer` binds by default, `--guidance` opts out, and drops the "lives only in that coder's session" paragraph.
- **Goal**: Edit only those sections; keep every other rule verbatim. The anchors table gains rows for the five new lines with their exact formats from U3/U4/U5. Phase 3's `coder_question` row reads: `answer` — binding Operator Decision carried into every later prompt of the group; use `--guidance` only for advice that changes no scope, acceptance or deliverable. A new short section in `triage-guide.md`, `When status reports Not Live`, gives the three cases (live again on its own → nothing; not live after a suspend with cures left → wait for the cure line; not live with cures exhausted or no suspend → `kill -INT -<pgid>` then `resume`).
- **Files**: `skills/orchestrator-run/SKILL.md`, `skills/orchestrator-run/triage-guide.md`
- **Symbols**: —
- **Depends-on**: U3, U5, U12
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - The phrases `phase has not changed across two heartbeats`, `worktree and transcript`, `20–30 min` and `slow heartbeat` no longer appear in `SKILL.md`; `not live for`, `machine suspend detected`, `suspend cure`, `cures exhausted` and `at most once an hour` do.
    Run: `! grep -nE "two heartbeats|worktree and transcript|20–30 min|slow heartbeat" skills/orchestrator-run/SKILL.md && grep -cE "not live for|machine suspend detected|suspend cure|cures exhausted|at most once an hour" skills/orchestrator-run/SKILL.md`
    Pass: the first grep finds nothing and the count is ≥ 5.
  - `triage-guide.md` contains `--guidance` and `When status reports Not Live`, and no longer contains `lives only in that coder's session`.
    Run: `grep -c -- "--guidance" skills/orchestrator-run/triage-guide.md && grep -c "When status reports Not Live" skills/orchestrator-run/triage-guide.md && ! grep -n "lives only in that coder" skills/orchestrator-run/triage-guide.md`
    Pass: both counts ≥ 1 and the last grep finds nothing.
  - Real-log oracle: every anchor line format quoted in the new table rows matches a line emitted by the U3/U4/U5 tests' captured run logs (the worker greps the exact strings out of `tests/test_liveness.py`'s asserted lines).
    Run: `grep -o "not live for[^|]*" skills/orchestrator-run/SKILL.md | head -1; grep -c "not live for" tests/test_liveness.py`
    Pass: the phrase is present in both files.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U14. evidence — a live-tier test proves Not Live end to end, and the plugin ships as 0.18.0 with the glossary committed

- **Summary**: [evidence] `tests/test_liveness_live.py` runs a real `smart-mcps-orchestrate run` with `[liveness] window_seconds = 20`, `SIGSTOP`s the real coder child, asserts `status` reports it `NOT LIVE` with `cpu flat` evidence within 60 s, `SIGCONT`s it and asserts the `live again` run-log line and a completed run; `.claude-plugin/plugin.json` becomes `0.18.0`; `CONTEXT.md`'s five new glossary entries are committed.
- **Goal**: The live test (marker `llm`, skipped without `claude`) reuses `tests/test_e2e_live.py`'s scratch-repo builder, writes `.orchestrator/config.toml` with `[liveness] window_seconds = 20` and `[session] model = "sonnet"`, launches `run` detached, polls `state.json` `live_pids` for a `claude` pid, sends `SIGSTOP`, polls `smart-mcps-orchestrate status <run>` every 5 s until a `liveness: NOT LIVE for` line appears (≤ 60 s), asserts the evidence contains `cpu flat` and no `suspend cure` line was logged (no suspend), sends `SIGCONT`, waits for `live again` in `logs/run.log` and for the run to complete. Version bump and glossary commit ride along.
- **Files**: `tests/test_liveness_live.py` *(new, medium)*, `.claude-plugin/plugin.json`, `CONTEXT.md`
- **Symbols**: —
- **Depends-on**: U5, U12
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - The live test passes against the installed CLI.
    Run: `uv run pytest tests/test_liveness_live.py -q -m llm`
    Pass: green (one real sonnet worker round is spent).
  - `.claude-plugin/plugin.json` reads `"version": "0.18.0"` and `CONTEXT.md` defines `Sign of Life`, `Liveness Window`, `Not Live`, `Operator Decision`, `Suspend Cure`.
    Run: `grep -c '"version": "0.18.0"' .claude-plugin/plugin.json && grep -cE "^\*\*(Sign of Life|Liveness Window|Not Live|Operator Decision|Suspend Cure)\*\*" CONTEXT.md`
    Pass: 1 and 5.
  - The full default suite and the UI suite are green on the merged result (the merge gate's own check; restated so the worker runs it before reporting).
    Run: `uv run pytest -q && (cd ui && npm test)`
    Pass: both exit 0.
- **Edge cases**: —
- **Non-goals / must-not**: —

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-event-stamp
    description: Stream-event hook, last assistant text, in-process ActivityRegistry fed by the session runner, SuspendCured exception, and heartbeat event facts
    slice: null
    files:
      - orchestrator/execution/streaming.py
      - orchestrator/execution/sessions.py
      - orchestrator/execution/heartbeat.py
      - orchestrator/execution/liveness.py
      - tests/test_streaming.py
      - tests/test_liveness.py
    size_hints:
      orchestrator/execution/liveness.py: large
      tests/test_liveness.py: medium
    symbols: []
    depends_on: []
    implements: ["ActivityRegistry"]
    consumes: []
  - task_id: u2-sign-of-life
    description: "[liveness] config, /proc helpers, LivenessProbe evaluating three signals per heartbeat tick into facts, and launch phases ending on the first assistant event"
    slice: null
    files:
      - orchestrator/execution/liveness.py
      - orchestrator/execution/heartbeat.py
      - orchestrator/config.py
      - docs/orchestrator-grouping-config.md
      - tests/test_liveness.py
      - tests/test_heartbeat.py
    symbols: []
    depends_on: [u1-event-stamp]
    implements: ["heartbeat.liveness-facts"]
    consumes: ["ActivityRegistry"]
  - task_id: u3-not-live-report
    description: Not Live enter/leave run-log lines, driver status line counting live and not-live groups, and the liveness_line and cures_exhausted_line helpers
    slice: null
    files:
      - orchestrator/execution/liveness.py
      - orchestrator/execution/driver.py
      - tests/test_liveness.py
      - tests/test_driver_liveness.py
    symbols: []
    depends_on: [u2-sign-of-life]
    implements: ["liveness-line"]
    consumes: ["heartbeat.liveness-facts"]
  - task_id: u4-suspend-detect
    description: SuspendMonitor sampling CLOCK_MONOTONIC vs CLOCK_BOOTTIME with a wall-clock fallback, run by the driver lock thread, stamping last_wake_at on the run heartbeat
    slice: null
    files:
      - orchestrator/execution/liveness.py
      - orchestrator/execution/driver.py
      - tests/test_liveness.py
      - tests/test_driver_liveness.py
    symbols: []
    depends_on: [u2-sign-of-life]
    implements: ["suspend-facts"]
    consumes: []
  - task_id: u5-suspend-cure
    description: Pid-tree kill after a suspend with no Sign of Life since the wake, warm resume in place through a review-loop worker-call wrapper, and a capped per-generation cures counter in state.json
    slice: null
    files:
      - orchestrator/execution/liveness.py
      - orchestrator/execution/review.py
      - orchestrator/execution/scheduler.py
      - tests/test_liveness.py
      - tests/test_suspend_cure.py
    size_hints:
      tests/test_suspend_cure.py: medium
    symbols: []
    depends_on: [u2-sign-of-life, u4-suspend-detect]
    implements: []
    consumes: ["suspend-facts", "ActivityRegistry"]
  - task_id: u6-activity-tail
    description: activity_tail and format_activity_tail helpers reading the last five tool calls from a session transcript
    slice: null
    files:
      - orchestrator/execution/transcript_events.py
      - tests/test_transcript_events.py
    symbols: []
    depends_on: []
    implements: ["activity-tail"]
    consumes: []
  - task_id: u7-observatory-liveness
    description: Observatory snapshot passes liveness facts and per-session activity tails through; the board renders a liveness line and the tail
    slice: null
    files:
      - orchestrator/observatory/runs.py
      - ui/src/types.ts
      - ui/src/components/GroupBoard.tsx
      - ui/src/components/GroupBoard.test.tsx
      - docs/observatory.md
      - tests/test_observatory_api.py
    symbols: []
    depends_on: [u2-sign-of-life, u6-activity-tail]
    implements: []
    consumes: ["heartbeat.liveness-facts", "activity-tail"]
  - task_id: u8-decision-ledger
    description: binding flag on the escalation response, decision_ledger derived from both escalation locations, rendered decisions section, Observatory body field, export pass-through
    slice: null
    files:
      - orchestrator/execution/decisions.py
      - orchestrator/model.py
      - orchestrator/execution/escalation.py
      - orchestrator/observatory/escalations.py
      - orchestrator/execution/export.py
      - docs/run-bundle-contract.md
      - tests/test_decisions.py
      - tests/test_escalation.py
    size_hints:
      orchestrator/execution/decisions.py: medium
      tests/test_decisions.py: medium
    symbols: []
    depends_on: []
    implements: ["decision-ledger"]
    consumes: []
  - task_id: u9-decisions-section
    description: Coder, handoff, reviewer and re-review prompts carry the ledger verbatim, the reviewer treats a decision as a spec amendment, the rewrite speccer template honours operator_decisions
    slice: null
    files:
      - orchestrator/execution/prompting.py
      - orchestrator/execution/review.py
      - orchestrator/prompts/coder.md
      - orchestrator/prompts/handoff.md
      - orchestrator/prompts/reviewer.md
      - orchestrator/prompts/re_review.md
      - orchestrator/prompts/rewrite_speccer.md
      - tests/test_decisions_carry.py
    size_hints:
      tests/test_decisions_carry.py: medium
    symbols: []
    depends_on: [u8-decision-ledger]
    implements: []
    consumes: ["decision-ledger"]
  - task_id: u10-content-filter-kind
    description: ContentFiltered exception from the session runner, caught by the worker-call wrapper and routed like a blocked coder quoting the last assistant message, never warm-resumed
    slice: null
    files:
      - orchestrator/execution/sessions.py
      - orchestrator/execution/review.py
      - tests/test_content_filter.py
    size_hints:
      tests/test_content_filter.py: medium
    symbols: []
    depends_on: [u1-event-stamp, u5-suspend-cure]
    implements: []
    consumes: []
  - task_id: u11-retry-release-note
    description: retry writes a released-by-operator failure note instead of clearing or keeping the stale text
    slice: null
    files:
      - orchestrator/execution/retry.py
      - tests/test_retry.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u12-cli-surfaces
    description: status prints the liveness line, cures-exhausted line and activity tail; answer gains --guidance; the rewrite provider passes operator_decisions to the speccer
    slice: null
    files:
      - orchestrator/cli.py
      - tests/test_cli_liveness_and_decisions.py
    size_hints:
      tests/test_cli_liveness_and_decisions.py: medium
    symbols: []
    depends_on: [u3-not-live-report, u6-activity-tail, u8-decision-ledger]
    implements: []
    consumes: ["liveness-line", "activity-tail", "decision-ledger"]
  - task_id: u13-skill-cadence
    description: Run-driver skill drops the manual wedge check, watches the new lifecycle lines, runs status at most hourly, and documents binding answers and the Not Live cases
    slice: null
    files:
      - skills/orchestrator-run/SKILL.md
      - skills/orchestrator-run/triage-guide.md
    symbols: []
    depends_on: [u3-not-live-report, u5-suspend-cure, u12-cli-surfaces]
    implements: []
    consumes: []
  - task_id: u14-evidence
    description: Live-tier test that SIGSTOPs a real coder child and reads Not Live from status, plugin version 0.18.0, glossary entries committed
    slice: null
    files:
      - tests/test_liveness_live.py
      - .claude-plugin/plugin.json
      - CONTEXT.md
    size_hints:
      tests/test_liveness_live.py: medium
    symbols: []
    depends_on: [u5-suspend-cure, u12-cli-surfaces]
    implements: []
    consumes: []
```
