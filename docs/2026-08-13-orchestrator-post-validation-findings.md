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

## Closed — all eight, 2026-08-13

Every item below was implemented on this branch after the doc was first written.
The section headings are kept intact so the original diagnosis stays readable
beside what was done about it.

| #   | closed by | what changed                                                                  |
| --- | --------- | ----------------------------------------------------------------------------- |
| P1  | `bd63cd1` | one orchestrator-owned cache root, env-driven, replacing the home enumeration |
| P2  | `e27b0e2` | `denial_error`/`denial_source` + a `DenialKind` classifier, one status kept   |
| P3  | `bc097c9` | the re-entry round announces itself before it blocks                          |
| P4  | `bc097c9` | run-scoped heartbeat over the base session                                    |
| P5  | `bc097c9` | `sys.stdout.reconfigure(line_buffering=True)` in `main()`                     |
| P6  | `298a420` | `UsageLimit` re-raised out of `_reenter`, no generation spent                 |
| P7  | `bd63cd1` | `provision_env` takes the cache env and `--all-extras`                        |
| P8  | `bc097c9` | `group` anchors a relative plan against `--repo`                              |

Three test tiers landed with them, chosen because the defects above were not
found by any amount of unit testing:

- **`tests/test_e2e_live.py`** (`-m llm`, never on a default `pytest`) drives a
  whole run of *real* processes under a *real* Landlock ruleset. Everything above
  `SessionRunner.preflight()` had been stub-only, which is precisely the seam this
  validation blew open. One session-scoped fixture, so N tests cost one run.
- **`tests/test_cwd_contract.py`** — the P8 class. `grep -rn "monkeypatch.chdir"`
  over `tests/` returned nothing beforehand: the cwd-vs-`--repo` contract had zero
  coverage, in a tool designed to be driven from another repo.
- **Confinement tests off `/tmp`** — a new `confined_root` fixture under
  `.orchestrator/tests/<uuid>` asserts with the *production* `system_paths`. The
  existing fixtures pass `system_paths=[]`, which proves each rule in isolation but
  discards the policy an operator actually gets (trap #2 below).

### What the live tier found on its first run

It earned its keep immediately. Three of these are corrections to beliefs this
codebase was carrying, none of which any unit test could have contradicted:

1. **`--allowedTools` adds capability; it does not restrict it.** With `Bash`
   omitted and no deny rule, the model ran `id` and returned its output. That
   reframes fix #5: shipping `DEFAULT_ALLOWED_TOOLS` on the run was right — a
   worker's capability no longer depends on the operator's personal settings — but
   it granted a *floor*, not a ceiling. The ceiling is `--disallowedTools`, which
   is why deny beats allow and why the safety rules live there.
2. **A withheld tool leaves nothing at all on the wire.** The CLI does not offer
   the tool, so no call is attempted and no `tool_result` arrives. P2's passive
   corroborator is therefore *structurally* unavailable for that kind, and
   `denial_source: tool_refused` is not a convenience — it is the only evidence
   that exists. That is what earns it a schema field.
3. **Model prose is not a protocol.** Two runs of the identical probe produced two
   different sentences for the same refusal. The classifier's prose patterns are
   best-effort by design and nothing rests on them: an unmatched phrasing degrades
   to `UNKNOWN`, which names both remedies, never to a wrong answer.

It also caught a **P6 gap for free**, by hitting a real limit mid-suite: the
wording is `You've hit your session limit · resets 1pm (Europe/Berlin)`, which the
first pattern set (written around `usage limit reached|<epoch>`) did **not** match
— so a limited run would have gone straight down the pointless-fork path P6 exists
to prevent. Every pattern in `_USAGE_LIMIT_RE` is now an observed string.

And the kernel-denial probe reproduced the original P1 failure verbatim, from the
operator's own `PreToolUse` hook: `Failed to initialize cache at /home/gbm1996/.cache/uv … Permission denied (os error 13)` — classified
`kernel_denied` from the wire signal alone, which is the case the last validation
misdiagnosed.

Two smaller things surfaced while fixing these and are worth recording:

- `RoundHeartbeat.mark_phase` only mutated memory, so a phase change did not reach
  `heartbeat.json` for up to 15 s — exactly when the process was about to block for
  a long time. It writes immediately now.
- The orchestrator *did* have an independent signal for P2 and was discarding it:
  `StreamingProcess._read_stdout` branched only on `assistant` and `result`, so
  every `user` event — where `tool_result` blocks arrive — was dropped.

## The eight items as originally diagnosed — all now closed

Kept verbatim, and **nothing here is outstanding**: this is the diagnosis that the
"Closed" table above refers back to, preserved so the reasoning behind each fix
stays readable next to the fix itself. See that table for which commit closed
which item.

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

**Superseded 2026-08-13 by auto-resume.** P6 fixed the *symptom* — the fallback
no longer burns a generation — but the limit still ended the run, and only a
human could restart it. That cost is now gone: the retry moved down to
`SessionRunner._call`, below where generations are counted, so a limited call
waits for the reset and replays itself. See "Auto-resume after a usage limit" in
`orchestrator/README.md`. P6's classification is what made it possible and stays
exactly as it was.

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

**Merge `orchestrator/run-r20260811-205146` to `main`** — which is also what makes
the editable-install `smart-mcps-orchestrate` on `PATH` carry any of this. Until
that merge, every invocation of the global command from another repo runs `main`,
i.e. pre-fix code: no streaming channel, no Landlock, none of the fixes. That is
trap #1 below in installed-tool form, and it is why the first attempt at a
cross-repo run failed before it started.

P1 and P2 are done, so the thing that turned a small policy gap into hours of
misattributed debugging is closed on both sides: the gap itself is far less likely
(one cache root, one config line for the exceptions), and when a denial does
happen it now names its own cause and remedy.

## Closed since: a usage limit no longer costs a human wait

The finding behind P6 was narrower than the problem. A limit did not just burn a
generation — it **stopped the run**, and the reset time the classifier had
already matched was discarded. One recorded run took ~2.7 days, "mostly
rate-limit-reset waits". Three things landed together:

1. **The run waits it out.** `execution/ratelimit.py` parses the reset time out
   of the limit prose and blocks until it passes, then retries the identical
   call. One gate per run, so concurrent groups join the same pause rather than
   each launching into an active limit. On by default; `--no-auto-resume` keeps
   the old behaviour exactly.
2. **The one-shot `claude -p` path can finally recognise a limit.** It raised a
   bare `LlmProcessError` and never called `is_usage_limit` at all, so `group`
   and every run-time spec rewrite treated a reset-in-40-minutes like a segfault.
3. **A pause reads as paused.** `run.log`, the group heartbeat's phase, and a new
   `runs/<id>/usage-limit.json` that drives an Observatory banner.

**Still unproven, deliberately:** the automated tiers cannot produce a genuine
account limit, so the reset-time parse has only ever run against strings captured
by hand. The **weekly** wording in particular is *unconfirmed* — the parse table
pins the behaviour chosen for a day-qualified reset, not a wording anyone has
seen. Capture the verbatim string the first time a real weekly limit fires and
add it to `tests/test_ratelimit.py`.

## What is still worth doing

Nothing from P1–P8 remains. The one thing the automated tiers cannot supply is a
**supervised live run against a real target repo** — the protocol that found the
original five defects. The live tier proves a run of real processes terminates,
commits and is confined; it cannot prove P1 against a genuine Node **and** Python
toolchain in the same repo, because its fixture has neither. The same gap now
covers the usage-limit parse above.
