---
title: Orchestrator post-validation findings and fix plan
date: 2026-08-13
branch: orchestrator/run-r20260811-205146
validated_against: drummAI (worktree `drummAI-practice-app`, branch `validation/practice-app`)
runs: r20260812-161122, r20260812-161423, r20260812-202855
---

# Orchestrator post-validation findings and fix plan

First end-to-end run of this branch against a real project. Baseline was green
throughout (953 tests) and the orchestrator was **non-functional** for four
separate reasons. This document records what was found, what was fixed, and what
is left — so the next session can plan on top of it rather than re-derive it.

Full evidence, with log lines and commands, is in the target repo at
`.orchestrator/notes-validation-2026-08-12.md`.

## The one sentence that matters

Every defect found was the same shape: **a complete, well-tested mechanism that no
real process had ever executed.** The tests all passed because they ran against a
stub that encoded the contract its author believed rather than the one the world
has.

## Fixed on this branch

Two commits, `4e872e2` and `e09d37c`. Suite: **957 passed, 4 deselected**, plus a
new opt-in live tier (`-m llm`) that talks to the real CLI.

| #   | defect                                                                                                                                          | effect before fix                                                                                                                      |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | streaming argv omitted `--verbose`; the CLI rejects `--print --output-format=stream-json` without it                                            | **no run could start** (exit 1 in ~20s)                                                                                                |
| 2   | prompt passed as `-p <text>` is ignored under `--input-format stream-json`; and the child does not exit on its own `result` while stdin is open | **run hung 4h05m** on 18s of CPU, blocked in `epoll_wait`                                                                              |
| 3   | no cache dir in the Landlock write allowlist                                                                                                    | **every worker died on its first tool call** (`uv` cannot init `~/.cache/uv`)                                                          |
| 4   | a linked worktree's git dirs are outside the worktree and were unwritable                                                                       | **no group could ever commit** — g1 finished U1+U2 with 279 tests passing and committed nothing                                        |
| 5   | `allowed_tools` shipped empty, so `--allowedTools` was never passed                                                                             | worker capability came from the operator's personal `~/.claude/settings.json`; no npm rule there meant **no Node project could build** |

Structural changes, which matter more than the five fixes:

- `tests/fake_claude.py` now **enforces the real preconditions** — rejects the argv
  the real binary rejects, and reads the prompt from stdin where the real binary
  reads it. It can no longer hide this family of bug.
- `tests/test_streaming_live.py` (`-m llm`) exercises the channel against the
  **real** CLI and asserts rounds *terminate*, not merely that they produce output.

## Verified working, under a real run

- **Confinement engages**: `confinement on (landlock abi 3)`, not the degrade path.
- **The argv carries the rules**, read from `/proc/<pid>/cmdline` of a live worker:
  `Bash(git stash:*)` plus `Edit|Write|MultiEdit|NotebookEdit(//…/projects/**/memory/**)`.
- **No worker wrote outside its worktree.** Checked three ways: every `file_path` in
  every transcript, mtimes across all memory dirs, and every write-capable Bash
  command. Zero violations. *This is the P0 that had bitten twice; it held.*
- **Workers test and commit under confinement** once (3) and (4) are fixed.
- **The heartbeat works**: launch line before the silence, `still <phase>, Nm elapsed` once a minute, phase advancing to `running (generation N round M)`,
  and `heartbeat.json` carrying `phase`/`phase_elapsed_s` with no
  `stalled`/`stuck`/`hung` field.
- **Interrupt and resume**: SIGINT drains the in-flight round, writes
  `interrupted at <ts> — no process is driving this run` above the group list,
  clears on resume, and the coder **resumes its existing session** rather than
  forking.
- **The Observatory is genuinely live**: `/events/log` and `/events/run` both open
  as single connections with no reconnect loop, the log pane matched `run.log`
  line-for-line, every tab renders, no console errors.
- **Merge integrity**: g1 merged 939 insertions across 4 files. `completed` was
  truthful.
- **Usage-limit handling**: a real account limit was classified `interrupted`
  (resumable), not terminal.

## Open — ranked, for the next session to plan against

### P1. The confinement allowlist is an unbounded enumeration

`system_write_paths()` now lists `~/.cache` and `~/.npm`. Next will be `~/.cargo`,
`~/.gradle`, `~/.m2`, `~/go/pkg`, and it will keep going. Each omission presents as
a mysterious `permission_denied` that looks like a worker failure.

**Proposal**: stop enumerating. Redirect each toolchain's cache *into the worktree*
via environment variables set on the worker (`UV_CACHE_DIR`, `npm_config_cache`,
`PIP_CACHE_DIR`, `CARGO_HOME`, …). Costs cold caches per group; buys a policy that
does not rot. Worth measuring the cold-cache penalty before committing.

### P2. `permission_denied` cannot be attributed

Three unrelated causes produce the same status and an opaque `denied_command`:
the operator's allowlist lacking a rule, Landlock denying a write, and the model
attempting something genuinely forbidden. This validation misdiagnosed one of them
with the source open.

**Proposal**: split the status, or have the worker report the *observed error* (an
EACCES from the kernel reads very differently from a permission-layer refusal) so a
reviewer and an operator can tell them apart.

### P3. A resumed group's first round is invisible as work

The re-entry LLM call is labelled `resuming the interrupted coder` for its whole
duration even when it performs a full round — writing files, running tests. During
that window `heartbeat.json` reads `generation: 0, round: 0, round_started_at: null`, and `round N: started`/`ended` are only logged together once it returns. An
operator watching a 20-minute re-entry sees `still resuming…, 18m00s elapsed` and
reasonably concludes it is wedged.

This is the round-atomic bookkeeping issue, scoped precisely: **it is the re-entry
path, not rounds generally.** Later rounds in the same group report correctly.

### P4. The base-session phase has no heartbeat

Run-level base-session establishment emits one line and then nothing. It is the
*first* long silence an operator meets, and it is the only one the heartbeat does
not cover.

### P5. Run stdout is block-buffered when redirected

`> run.log` leaves the file empty for the life of the run, so the header — the one
line stating whether confinement is on — is invisible to anyone who backgrounds the
run, and indistinguishable from a hang. `PYTHONUNBUFFERED=1` is a workaround;
`flush=True` on the header prints, or `-u` in the entry point, is the fix.

### P6. A usage limit triggers a pointless fork

A warm resume that fails on an account limit falls back to forking a fresh
generation, which fails identically and burns a generation. "Session unusable" and
"account exhausted" need distinguishing before the fallback.

### P7. Group worktree venvs lack the operator's optional extras

drummAI's `audio_separator` extra is installed in the main checkout but not in a
fresh worktree venv, so two tests fail for reasons unrelated to the group's work —
and its reviewer cannot tell that apart from a regression. One worker also hit an
`EXDEV` cache-rename failure from `uv sync` and had to re-bootstrap pip by hand.

### P8. `group` resolves the plan path against cwd, not `--repo`

A repo-relative plan path fails with `plan document not found`. Minor, but it is the
first command anyone runs.

## Two traps for whoever works on this next

1. **`uv run` resolves against the cwd**, and this session's cwd silently reset to
   the main checkout twice — once launching a run, once running `status`. Both
   produced confident, wrong conclusions about the branch. Always `cd` to the
   worktree in the same invocation. **The tell**: if the header lacks
   `confinement on (landlock abi …)`, you are running `main`.
2. **A test whose fixture lives under `/tmp` proves nothing about the confinement
   boundary**, because `/tmp` is blanket-writable in the production policy. The
   first draft of the git-dir test passed with the bug present for exactly this
   reason. Pare `system_paths` when asserting a boundary.

## What the run produced for drummAI

Real merged work, not a throwaway:

- `b95e6f0` refactor(score): note-value spelling becomes data, LilyPond a formatter
- `807ab77` feat(chart): addressable chart from a transcript — 430 lines plus 346
  lines of tests
- `ab51cf8`, `89c0438` (g8, committed but not yet merged): the `app/` API and a
  fresh `web/` client — 2055 insertions across 28 files, with `calibration-web/`
  left byte-identical as the amended plan required

## Merge recommendation

**Merge `orchestrator/run-r20260811-205146` to `main` after the two fix commits**
(`4e872e2`, `e09d37c`) — not before. The branch's own features are sound; they were
simply never run. With the fixes they work, and A3 — the P0 that had bitten twice —
holds under real load.

P1 and P2 should be planned before the next long unattended run, because together
they are what turns a small policy gap into hours of misattributed debugging.
