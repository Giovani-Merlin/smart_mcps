---
date: 2026-09-16
topic: stall-detection-and-decision-carry
---

# Stall detection and decision carry — Requirements

## Summary

The orchestrator tracks the driver process, not the work: it knows its own
heartbeat is ticking but not whether the coder child behind it is doing
anything, and it forgets what the human decided the moment a generation
retires. This work gives every running group a **Sign of Life** read from the
child itself (stream events, a tool subprocess, CPU ticks), reports **Not
Live** with evidence everywhere an operator looks, cures the one observed cause
(a machine suspend) in-process, and turns every `coder_question` answer into an
**Operator Decision** that every later coder and reviewer prompt of that group
carries verbatim. Two smaller items ride along: a content-filter API error gets
its own failure route, and `retry` stops leaving a stale failure line in
`status`. No wall-clock limit on a round or on the work is introduced (R7
stands).

## Problem Frame

- **r20260908 g8, three times.** A laptop suspend left the `claude` child
  alive with a dead socket. The driver kept heartbeating, `status` said
  `progressing`, the phase stayed `starting the coder` for 18m49s of counted
  time. Cure was a manual `kill -INT -<pgid>` + `resume`, four times over three
  days. The seed's own diagnosis ("the child died") is contradicted by the code:
  `proc.wait()` (`streaming.py:338`) returns the instant a direct child dies,
  so the child was alive and hung. A liveness check on the pid alone would have
  seen nothing.
- **r20260913 g16, the inverse.** `still starting the coder` for 14 minutes
  while the worktree was being edited: `review.py:517` marks that phase and then
  blocks on the whole first round, so the label is wrong by design.
- **The run-driver skill guesses.** Its manual wedge check compares phases
  across two `status` calls and stats the worktree and transcript. It has
  declared a run dead after hours of no updates while the run was fine, and it
  spends tokens on heartbeat polling the human did not ask for.
- **r20260913 g5.** The human cut a render from six chapters to four inside a
  `coder_question` answer. Generation 1 retired on context; the generation-2
  reviewer saw only the spec, called the four chapters "self-invented" and
  forced a revert. Today a `coder_question` answer reaches only the warm
  session's next prompt (`review.py:1441`); the reviewer prompt judges against
  the spec alone (`reviewer.md:4`); the handoff prompt carries reviewer items
  and over-cap grant notes but no answers (`review.py:1400`).
- **r20260907 g1.** `API Error: Output blocked by content filtering policy`
  is a generic `SessionError` (`sessions.py:774`), so the warm resume repeated
  it, and the fallback fork repeated it again.
- **Prior art.** The heartbeat (`heartbeat.py`) already records the current
  phase and its age as facts only, on purpose: nothing persists a `stalled`
  state, because a persisted state becomes something code branches on. The
  stream reader (`streaming.py:213`) already parses every event type. `LivePid`
  records pid and `/proc` start time. Escalation records under `escalations/`
  already hold every question and answer keyed by group. Everything here
  composes these; nothing replaces them.

## Key Decisions

- **No round timeout, no kill on silence.** R7 stands and is reaffirmed. The
  only cap introduced is the Liveness Window, which bounds the age of the
  freshest Sign of Life, never the round. Killing a child because it was quiet
  for N minutes was proposed and rejected: a healthy round can legitimately run
  for hours.
- **Sign of Life is three signals, any one suffices.** A stream-json event of
  any type, a live non-zombie process whose parent is the coder child (a tool
  call in flight), or the child's own CPU ticks advancing. Assistant-turns-only
  was rejected because a ten-minute Bash call would read as silence.
  Socket-state inspection (`wchan`, `/proc/net/tcp`, keepalive) was rejected on
  external advice as brittle; hung I/O already shows as flat CPU plus no events.
- **Not Live is reported, and cured only after a suspend.** Report-only was
  coherent but leaves the overnight case for the morning; cure-on-any-not-live
  was rejected with the timeout. A detected machine suspend plus no Sign of
  Life since the wake is a trigger a slow round cannot fire.
- **A cure is not a Re-entry.** It gets its own capped counter. Reusing
  `reentry_count` would quarantine a nightly-suspending laptop's groups in
  three days, exactly the r0908 failure that 0.17.2 fixed for operator signals.
- **The run-driver skill stops guessing.** Its manual wedge check is deleted.
  It reacts to run-log lifecycle lines through Monitor and runs `status` at
  most hourly as a fallback. The orchestrator process does the liveness work.
- **The decision ledger is derived, not written.** Rendered at prompt time from
  the escalation records already on disk plus one binding flag on the answer.
  A separate `decisions.md` was rejected as a second copy that can drift.
- **`coder_question` answers bind by default.** A question the coder stopped
  to ask is by nature a decision. `--guidance` opts out. Opt-in `--binding` was
  rejected because a forgotten flag repeats r0913. `retry` notes stay one-shot;
  blocked/too_hard answers already become the spec through the speccer.
- **Decisions travel as a prompt section, not as spec text.** `## Operator
  decisions (binding)` is rendered verbatim into every later prompt of the
  group; the spec and `spec_sha256` stay untouched, so resume forking is
  unaffected and no speccer call is spent.
- **A binding decision amends the spec both ways for the reviewer.** Work that
  follows it is never self-invented; work that contradicts it is
  `changes_required` citing the decision. Tolerate-only was rejected because a
  coder that ignores a scope cut would still pass.
- **Content-filter errors get their own kind, no warm resume.** Routed like
  `coder_blocked` with the last assistant message quoted, so a human (HITL on)
  or the rewrite path (HITL off) sees the actual blocked text instead of a
  third repetition.
- **Evidence is unit + live tier + one real run.** Both halves of this seed
  have the "mechanism tested, wiring absent" shape the Landlock work already
  fell into once.

## Requirements

### Sign of Life and Not Live

- R1. `event-stamp` — **Every stream event stamps activity.** The stream reader
  records, for the running child, the wall-clock time and type of the last
  stream-json event of any kind (`assistant`, `user`, `system`, `result`,
  partial-message deltas). The heartbeat snapshot exposes it as
  `last_event_at` and `last_event_type`. No new thread; the reader thread
  already has every line in hand.
- R2. `sign-of-life` — **Three signals, evaluated every heartbeat tick.** On each
  tick the heartbeat evaluates, for the group's current child: (a) a stream
  event within the Liveness Window, (b) at least one live, non-zombie process
  in the child's process group whose parent pid is the child, (c) the child's
  `utime+stime` from `/proc/<pid>/stat` advanced since the previous tick. It
  records `last_sign_of_life_at`, which signal produced it, and a one-line
  evidence summary (event age, tool child command head and age, CPU flat or
  advancing) in `heartbeat.json`. Facts only: no `stalled`, `not_live`, or
  similar boolean is persisted as group state. Read errors (`ENOENT`, a process
  that exited between listing and reading) are treated as "no signal from that
  source this tick", never as an exception that can fail the round.
- R3. `liveness-window` — **A cap on signal age, never on work.** A config value
  `liveness.window_seconds`, default 600, defines Not Live: no Sign of Life
  for a whole window. It applies to coder and reviewer rounds alike. The
  `launching` phase (R5) uses the same window measured from spawn. Nothing in
  this work limits a round's or a group's wall-clock duration, and the
  `streaming.py` and `sessions.py` R7 notes are updated to say so explicitly.
- R4. `not-live-report` — **Not Live is visible everywhere an operator looks.**
  Entering Not Live writes one run-log Lifecycle Event
  (`group <gid> generation <n>: not live for <age> in <phase> — <evidence>`)
  and leaving it writes one more; the line is not repeated every tick. `status`
  prints, per active group, one liveness line with the evidence summary (a live
  group prints `live: <signal> <age> ago`). The driver status line stops
  saying `progressing` from the heartbeat mtime alone; it says how many active
  groups are live and how many are not. The Observatory shows the same line
  from `heartbeat.json`.
- R5. `honest-phases` — **The phase names what is happening.** `starting the
  coder`, `forking the base session` and `resuming the interrupted coder` last
  only until the first stream event; the phase then becomes `round <r>
  running`. The same split applies to reviewer launches.

### Suspend detection and cure

- R6. `suspend-detect` — **A machine suspend is a recorded fact.** Each heartbeat
  tick samples `CLOCK_MONOTONIC` and `CLOCK_BOOTTIME`; a boot-time delta
  exceeding the monotonic delta by more than `liveness.suspend_gap_seconds`
  (default 60) marks a suspend. Fallback for hosts where both clocks freeze
  (WSL2 with the VM paused): a wall-clock gap between two ticks larger than five
  tick intervals also marks a suspend. A detected suspend writes one run-log
  Lifecycle Event (`machine suspend detected: <gap>`) and stamps
  `last_wake_at` on the run's heartbeat.
- R7. `suspend-cure` — **Cure only after a suspend.** When a suspend was
  detected and a child has had no Sign of Life since `last_wake_at` for a whole
  Liveness Window, the driver, in-process: sends `SIGTERM` to the child's
  process group, waits `liveness.kill_grace_seconds` (default 10), sends
  `SIGKILL` to whatever remains, and warm-resumes the same session id in the
  same generation through the existing Warm Resume mechanism, in-process, with
  no scheduler Re-entry and no operator `resume`. Every step is a run-log
  Lifecycle Event naming the group, generation and session. The Not Live
  condition alone, without a detected suspend, never triggers this.
- R8. `cure-cap` — **A cure is counted, capped, and is not a Re-entry.** Each
  group carries `cures` per generation in `state.json`. A cure never increments
  `reentry_count`. After `liveness.max_cures_per_generation` (default 2) cures
  with no Sign of Life following the last one, the driver stops curing that
  group and only reports, and `status` says so with the `kill -INT` + `resume`
  command an operator would run.

### Operator visibility

- R9. `activity-tail` — **`status` shows what the worker is doing.** For every
  active group, `status` prints the last five worker actions read from the
  session transcript: time, tool name, first line of the tool input, and
  whether the call has returned. The Observatory shows the same tail from the
  transcript it already reads. Read-only; nothing is persisted for it.
- R10. `skill-cadence` — **The run-driver skill watches events, not
  heartbeats.** `skills/orchestrator-run/SKILL.md` and `triage-guide.md` drop
  the manual wedge check (phase comparison across two `status` calls, worktree
  and transcript mtime checks). The skill's Monitor conditions include the new
  `not live`, `machine suspend detected` and cure lines; its fallback `status`
  runs at most once an hour. The documented manual cure (`kill -INT -<pgid>`,
  then `resume`) is reserved for a group that `status` reports Not Live with
  cures exhausted or with no suspend detected.

### Operator decisions

- R11. `decision-ledger` — **Binding answers are derived from the escalation
  records.** For a group, the ledger is the ordered list of its answered
  `coder_question` escalations whose answer is marked binding: escalation id,
  time, the question as asked, and the answer text verbatim. It is computed at
  prompt-render time from `escalations/`; no separate ledger file is written,
  and the run directory's contract does not change beyond one flag on the
  answer record.
- R12. `binding-default` — **`coder_question` answers bind unless opted out.**
  `answer` on a `coder_question` records `binding: true` by default;
  `answer --guidance` records `binding: false` and behaves exactly as today
  (warm session only). `retry` notes stay one-shot `## Operator note`s;
  answers on blocked, too_hard and structural escalations keep their existing
  speccer route. The run-driver skill's answer instructions say which to use
  and default to binding for anything that changes scope, acceptance or
  deliverables.
- R13. `decisions-section` — **Every later prompt of the group carries the
  ledger.** When the ledger is non-empty, the fresh coder prompt, the handoff
  prompt, the reviewer prompt (every round), and the speccer rewrite input
  each carry a `## Operator decisions (binding)` section listing every entry
  verbatim with its escalation id and time. The group's spec text and
  `spec_sha256` are not modified by a decision.
- R14. `reviewer-amend` — **The reviewer treats a decision as an amendment.**
  `reviewer.md` states that a binding decision amends the spec: work that
  follows one is never judged self-invented or out of scope, and work that
  contradicts one is `changes_required` with the required change citing the
  decision's escalation id.

### Smaller items

- R15. `content-filter-kind` — **A content-filter error is its own failure.**
  An error envelope whose text matches the content-filter phrase raises a
  distinct exception type. It is never warm-resumed. It is routed like a
  `coder_blocked` report: the escalation quotes the last assistant message
  before the error; under HITL off it takes the existing rewrite route with a
  context surprise naming the filter.
- R16. `retry-release-note` — **`retry` leaves no stale failure line.**
  `retry` replaces the group's `failure` text with `released by operator at
  <time>; resume to continue`, so `status` shows the release until the group
  actually re-enters.

### Evidence

- R17. `evidence` — **Wired, not just tested.** Unit tests cover the Sign of
  Life predicate against a fake `/proc` tree, suspend detection against fake
  clocks (both the clock-divergence and the sample-gap paths), the cure cap,
  ledger rendering into each prompt kind, and the content-filter routing. A
  live-tier test stops a real coder child with `SIGSTOP`, asserts `status`
  reports it Not Live within a shortened window with the expected evidence,
  then resumes it. One learning_podcast run, driven by the run-driver skill,
  closes the work: its notes file must show a `status` liveness line, an
  activity tail, and one `coder_question` answer appearing verbatim in a
  later generation's reviewer prompt.

## Non-Goals

- Any wall-clock limit on a round, a generation, or a group's work.
- Killing or resuming a child because it was silent, without a detected
  suspend.
- A persisted `stalled` / `not_live` group state, or a new `EscalationKind` for
  stalls.
- Socket-state inspection (`/proc/<pid>/wchan`, `/proc/net/tcp`, TCP
  keepalive) as a liveness signal.
- A hand-editable decisions file per group, or folding decisions into the spec
  text with a new `spec_sha256`.
- Binding `retry` notes, grant notes, or blocked/too_hard answers through the
  ledger; their existing routes are unchanged.
- Counting a Suspend Cure toward `max_reentries`.
- The Observatory acting on liveness (killing, resuming); it shows only.
- Changing the run-driver skill's HITL default or escalation handling beyond
  the watch cadence and the answer flag.

## Open Questions

None. Two assumptions are recorded instead: the CLI's own retry backoff on an
overloaded API is assumed to stay under ten minutes of total silence (the
window is config-overridable if a run proves otherwise), and every tool
subprocess is assumed to stay in the coder child's process group, which the
existing `kill -INT -<pgid>` cure already relies on.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-09-16-stall-detection-and-decision-carry-requirements.md`.
