---
title: "Brainstorm seed A — stalled work is invisible, and operator decisions don't survive a generation"
date: 2026-09-15
status: seed (input to /orchestrator-brainstorm)
sources:
  - learning_podcast/docs/orchestrator-feedback.md §6, §9
  - learning_podcast/.orchestrator/notes-r20260908-224537.md
  - learning_podcast/.orchestrator/notes-r20260913-162134.md
  - learning_podcast/.orchestrator/notes-r20260907-123644.md
  - .orchestrator/notes-sweep-2026-09-15.md (B1, B3, B4, B13, B14)
---

# A — Stalled work is invisible, and operator decisions don't survive a generation

Two problems, in one seed because they share a root: the orchestrator tracks
**the driver process**, not **the work**. It knows the driver is alive; it
does not know whether the coder behind it is doing anything, or what the
human decided along the way.

Already shipped in 0.17.2 and **not** in scope here: a resume after an operator
`kill -INT`/`-TERM` no longer counts toward `max_reentries` (the r0908 g8
quarantine), `answer --text-file`, and the run-driver skill's manual wedge
check.

______________________________________________________________________

## Problem 1 — A stuck step looks healthy (B1, B3)

### What happened

- **r20260908 g8, twice.** A machine suspend killed the coder child. The
  driver survived and kept heartbeating (`progressing (12s since last heartbeat)`) while the step stayed `starting the coder` for **18m49s of
  counted time**. Every other group took seconds. `status` said the run was
  healthy. The only cure was `kill -INT -<pgid>` + `resume`, repeated four
  times over three days, which is what quarantined g8.
- **r20260913 g16, the inverse.** The status line read `still starting the coder` for 14 minutes while the worktree was being actively edited. That
  line was misleading, not a wedge.

### What the code does today (main 055abfa + 0.17.2)

- `review.py:~517` marks the step `starting the coder` and then blocks on
  `asyncio.to_thread(self._launch_call(), …)`. That call is **the entire
  first round**, not a launch. The step only changes to `coder working toward a report` after round 1 returns. So on a long first round the label
  is wrong by design, which explains g16.
- `streaming.py` header: *"No per-round timeout … A hung stream is only
  detected by the process itself exiting or by the caller closing
  stdin/killing it."* After a suspend, the child is dead but the reader
  thread / `proc.wait()` never returns. That explains g8.
- An `on_turn` callback already fires **once per assistant turn, as the round
  runs** (`_make_coder_on_turn`), and the heartbeat already tracks the current
  step and how long it has lasted (`heartbeat.py`, `_phase_since`). The raw
  signal for "is the work advancing" exists; nothing compares it against an
  expectation.
- `LivePid` (0.17.0) records pid + `/proc` starttime for each child, so
  checking whether a child is really still running is cheap.

### Directions to brainstorm

1. **Last-activity clock per group.** Stamp `last_turn_at` from `on_turn`, and
   optionally transcript-jsonl and worktree mtimes. `status` shows `no activity for Ns in <phase>`. The heartbeat reader decides it is stalled;
   nothing persists `stalled: true` (see the `heartbeat.py` docstring for why).
2. **Child liveness watchdog.** A thread that checks `LivePid` every N seconds.
   If the child is gone but `wait()` has not returned, close the reader and
   raise an explicit `SessionError("coder child vanished")` so the normal
   re-entry path runs *in-process*, with no operator restart at all.
3. **Suspend detection.** A large gap between monotonic and wall-clock time
   across two heartbeats means the machine slept. On wake, re-check every
   child. This is the cheapest precise signal for the laptop case.
4. **Honest step names.** Split `starting the coder` into `launching` (until
   the first `on_turn`) and `coder round 1 running`.
5. **Per-step expectations.** A table (config-overridable) of "this step should
   produce activity within X", so `status` can say `phase stalled` rather than
   leave the human to infer it.

### Open questions

- Should a vanished child count against `max_reentries`? The 0.17.2 rule: a
  signal stop doesn't count, a crash does. A suspend is neither.
- Is `on_turn` enough, or do long tool calls (a 10-minute Bash) need
  tool-start events too, so they don't read as silence?
- Does the Observatory need the same signal (it reads the heartbeat already)?

______________________________________________________________________

## Problem 2 — Operator decisions don't reach later generations (B4)

### What happened

**r20260913 g5.** The human cut a render from six chapters to four inside a
`coder_question` answer. Generation 1 retired on context (382k). The
generation-2 reviewer saw only the spec, called four chapters "self-invented",
and forced a revert (`e468c8d`). The driver then had to spend a spec rewrite
quoting the human verbatim to undo it.

### What the code does today

| channel                        | where the text goes                                                                                  | survives a new generation?                       |
| ------------------------------ | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `answer` on `coder_question`   | the warm session's next prompt, verbatim                                                             | **no** — it lives in that session's context only |
| `answer --action retry`        | `_operator_notes`, one-shot `## Operator note` (`review.py:_apply_operator_note`, cleared after use) | **no** — one-shot by design                      |
| `answer` on blocked / too_hard | folded into a speccer rewrite as an `[operator]` surprise                                            | yes — it becomes the spec                        |

The reviewer judges against the spec only, so any decision not in the spec is
invisible to it.

### Directions to brainstorm

1. **A decision ledger per group.** Append every operator answer (question +
   answer text + escalation id) to `groups/<gid>/decisions.md`. Every coder
   *and reviewer* prompt from then on carries it under `## Operator decisions (binding)`. No LLM call, no rewrite budget.
2. **Classify at answer time.** `answer --binding` (or the skill asks) marks a
   scope/acceptance change. Binding decisions go to the ledger; plain guidance
   stays one-shot.
3. **Fold into the spec without the speccer.** Append the decision verbatim to
   the spec's verification section as an amended item, and record the new
   `spec_sha256` (0.17.0 E) so a resume forks correctly.
4. The handoff prompt (`handoff.md`) for a retired generation should carry the
   ledger too. Check whether it carries anything from escalations today.

### Open questions

- Does a binding decision need the human to confirm the reworded verification
  item, or is a verbatim quote enough? (r0913: a verbatim quote settled it.)
- Should the reviewer be told to reject work that *contradicts* a ledger entry,
  not just accept work that follows it?

______________________________________________________________________

## Smaller items that fit this seed

- **B13 — content-filter API error.** `API Error: Output blocked by content filtering policy` killed g1 twice at the identical point (r20260907-123644).
  The warm resume repeated it, and the fallback fork repeated it again. Treat
  it as its own failure type: no warm resume, escalate at once with the last
  assistant message quoted. The fix in that run was a generator script plus a
  committed data file (see seed B, P7).
- **B14 — `retry` subcommand vs `status`.** `retry` prints the release message
  while `status` still shows the old failure until the next `resume`. Either
  clear the failure line in `retry`, or have `status` say `released, resume to continue`.

## Evidence index

| id                             | run              | where                                                     |
| ------------------------------ | ---------------- | --------------------------------------------------------- |
| g8 wedge 18m49s                | r20260908        | notes §"2026-09-09 15:27 → 09-11 09:02: wedge"            |
| g8 quarantine after 4 restarts | r20260908        | notes §"2026-09-12 15:58" (fixed in 0.17.2)               |
| g16 misleading step label      | r20260913        | notes line "g16 kept still starting the coder for 14 min" |
| g5 scope revert                | r20260913        | escalations 569e014c918d → 70dc973ffee1                   |
| content filter                 | r20260907-123644 | notes §"Incident 1" + §"Root cause"                       |
