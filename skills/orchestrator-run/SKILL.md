---
name: orchestrator-run
description: Launch, watch, and resolve an orchestrator run as the run-driver session — preflight the repo, start `smart-mcps-orchestrate run` detached with HITL on, triage every escalation yourself (fix env/config/plan and `retry`, `answer` from the docs, ask the human only for product decisions), and write up the outcome. Use when the user wants to run, resume, drive, or babysit an orchestrator run.
user-invocable: true
argument-hint: "[plan path | run id to resume] [--grouping NAME] [--concurrency N]"
---

# orchestrator-run

You are the **run-driver session**. You launch the run, watch it, and resolve
whatever it escalates. Workers never talk to the human — **you** do, and only
for decisions that are genuinely theirs (product scope, a boundary judgement).
Everything else — a broken environment, a missing data dir, a config typo, a
plan item that turned out self-referential — you fix yourself and relaunch.

Until plugin 0.14.0 this role was named in three docstrings ("the main session
tails run.log and watches pending escalations") and implemented nowhere; a
stuck group had exactly two resolvers, the speccer rewrite or a human typing
`answer`. This skill is the third resolver. Read
`skills/orchestrator-run/triage-guide.md` before Phase 3 — it is the playbook.

Invariants:

- **A blocked group holds its dependents.** Triage promptly; never leave an
  escalation pending while you explore something unrelated.
- **`answer` is write-once.** "already answered" is a fact, not an error to
  retry with different text.
- **Never `git clean` / `reset --hard` a group's worktree.** Stranded,
  uncommitted work is the group's progress (see the recovery lesson in
  `triage-guide.md`).
- **Every finding goes to `.orchestrator/notes-<run_id>.md` as you go** — not
  the `/tmp` scratchpad, which a restart wipes (CLAUDE.md).
- The CLI's `retry` **subcommand** (release a terminally failed group) and the
  `answer --action retry` **escalation action** (relaunch the same spec) are
  different things; this skill uses both and names them in full.

Input: `$ARGUMENTS` — a plan path (new run), a run id (resume), or nothing
(resume the single unfinished run, else ask which plan).

______________________________________________________________________

## Phase 0 — Preflight (refuse to launch until green)

Run from the repo root. Each check is cheap; a failure here would otherwise
surface twenty minutes into the run as a `coder_blocked` on every group.

1. **Commits exist, tree is clean.** `git rev-parse --verify HEAD`;
   `git status --porcelain`. A dirty tree is allowed only if the human
   accepts it explicitly — workers fork from the launch commit, so anything
   uncommitted is invisible to them.
2. **Config exists and names the data.** `.orchestrator/config.toml` must
   exist. If the plan names data inputs (a corpus, a PDF, a model), then
   `[workspace] data_dirs` must list a directory covering each, and each listed
   directory must exist and be non-empty (`find <dir> -type f | head -1`).
   `[session] provision_on_failure = "warn"` is only acceptable with a stated
   reason — the default `fail` is what stops a run whose venv never built.
   `[docs] formats` must be non-empty unless the human declines a report: with
   no formats `finish` writes no report and the one-pager has nowhere to land
   (`run` also warns at launch). Every deliverable the plan leaves on disk
   (EPUBs, renders, media) must land under a `data_dirs` path — anything
   git-ignored elsewhere is only rescued as a capped copy into
   `.orchestrator/runs/$RUN/groups/<gid>/ignored-outputs/`.
3. **Grouping exists and is current.** `smart-mcps-orchestrate groupings`.
   If the plan file is newer than the grouping's `groups.json`, or the plan
   was deepened since, regenerate: `smart-mcps-orchestrate plan-check <plan>`
   then `smart-mcps-orchestrate group <plan>`.
4. **The environment builds on the launch branch.** Read
   `[session] provision_args` (default `["--all-extras"]`) and run
   `uv sync <those args>` yourself. This catches the `tts`-extra class of
   failure before the CLI does, with the full stderr in hand. If it fails,
   fix `pyproject.toml`/`uv.lock` and commit; do not launch on a red build.
5. **No unfinished run is silently abandoned.** `smart-mcps-orchestrate status`
   (no id shows the single unfinished run). If one exists, `resume` it — or,
   if the human clearly wants a fresh run, say so and pass `--run-id`
   explicitly. A non-tty launch skips the CLI's `[y/N]` prompt, so this check
   is yours, not the CLI's.

Report the five results in one short block. Refuse to continue on any red
item; do not "launch and see".

## Phase 1 — Launch, detached

Pick the run id up front so every path is known before the process exists:
`RUN=r$(date +%Y%m%d-%H%M%S)`.

```sh
mkdir -p .orchestrator/runs/$RUN/logs
setsid nohup smart-mcps-orchestrate run --repo "$(pwd)" --run-id $RUN \
  --hitl --intensity on_stuck --escalation-timeout 14400 \
  [--grouping NAME] \
  > .orchestrator/runs/$RUN/logs/driver.log 2>&1 < /dev/null &
echo $!
```

For a resume: the same, with `resume <run_id>` in place of `run … --run-id`.

- **Always the `smart-mcps-orchestrate` console script, never
  `python -m orchestrator.cli`.** When the run targets another repo the
  orchestrator package is not on that repo's path, and `python -m` resolves
  to whatever interpreter is current there — the console script is the one
  entry point that works from any checkout.

- **Serial is the default — do not pass `--concurrency`.** Omitting it leaves
  the config default of 1: each group's worktree is cut from the integration
  tip at its ready→running transition, so groups stack on merged work,
  cross-group merge conflicts stay rare, and a usage-limit hit costs at most
  one in-flight group. If the grouping looks like it would parallelise well
  (independent groups in the same wave, and the human is not rate-limited),
  **ask the human** before raising it — say how many groups could run at once
  and what it buys — and pass `--concurrency N` only on an explicit yes. A
  concurrency in `$ARGUMENTS` or already set in `.orchestrator/config.toml`
  counts as that yes; use it as given.

- `run` **blocks** until the run ends; there is no `stop` subcommand.
  Stopping = `kill -INT -<pgid>` (the process group `setsid` created), which
  the CLI logs as `run <id> interrupted (SIGINT)` and leaves resumable.

- **Always pass `--escalation-timeout`.** `timeout_s = None` blocks forever;
  if this session dies, the timeout (`on_timeout = autonomous`) hands the
  group to the speccer instead of wedging the run. Four hours is the default
  budget; shorten it if the human will not be around.

- Record `run id`, `pid`, launch command, and launch commit in
  `.orchestrator/notes-<run_id>.md` immediately.

- Confirm liveness within a minute: `smart-mcps-orchestrate status $RUN`
  should read `a process is driving this run (pid …)`, and `logs/run.log`
  should contain `run <id> started with HITL: intensity=on_stuck`.

## Phase 2 — Watch, event-driven

Never poll on a fixed interval. Use the `Monitor` tool with an until-condition
that fires on any of:

- **(a)** a new `.orchestrator/runs/$RUN/escalations/request-*.json` with no
  matching `response-*.json` (the primary signal);
- **(b)** the run process exiting (`kill -0 <pid>` fails);
- **(c)** a new terminal group line in `logs/run.log` —
  `group <gid>: completed`, `group <gid>: failed (…)`,
  `group <gid>: resolved (…)`, `group <gid>: terminal failed — …`,
  `run <id> aborted by operator: …`, `run <id> interrupted (SIGINT)`.

There is no manual wedge check to run here anymore. The Liveness probe
watches the worker child itself — a real Sign of Life inside a configurable
window, not the driver's own heartbeat mtime — and writes evidence straight
into `logs/run.log` the moment a group's status actually changes. Do not
compare phases by hand across status calls or poll the worktree/transcript
yourself; the probe already does both and only speaks up when something
happened. The one recurring check left is a `status $RUN` fallback, run at
most once an hour, purely as a backstop for a missed Monitor condition — the
liveness lines below are the primary signal. A run paused on a usage limit
announces itself on the group heartbeats and reads as *paused*, not Not
Live — wait it out.

Greppable anchors, all in `logs/run.log`:

| event                | line                                                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| escalation raised    | `ESCALATION <id> [<kind>] group <gid>` (+ `blocks …` if any)                                                               |
| escalation answered  | `ESCALATION <id> answered: <action>`                                                                                       |
| escalation timed out | `ESCALATION <id> timed out → <on_timeout>`                                                                                 |
| retry relaunch       | `group <gid> generation <n>: relaunching on the same spec …`                                                               |
| spec rewrite         | `group <gid> generation <n>: rewriting spec (<why>) …`                                                                     |
| coder launched       | `group <gid> generation <n>: coder launching, …`                                                                           |
| round ended          | `group <gid> generation <n> round <r>: ended (<status>)`                                                                   |
| reviewer verdict     | `… reviewer verdict <status>`                                                                                              |
| coder retired        | `group <gid> generation <n>: coder retired (<reason>)`                                                                     |
| session cost         | `group <gid> generation <n>: coder session ended — <r> rounds, $<usd>`                                                     |
| usage-limit pause    | `usage limit: pausing …` / `usage limit: resuming …`                                                                       |
| group not live       | `group <gid> generation <n>: not live for <age> in <phase> — <evidence>`                                                   |
| group live again     | `group <gid> generation <n>: live again: <signal> <age> ago` (or `child exited`)                                           |
| machine suspend      | `machine suspend detected: <gap>`                                                                                          |
| suspend cure         | `group <gid> generation <n>: suspend cure <k>/<max> — no sign of life since wake at <ts>; killing session <sid> pid <pid>` |
| cures exhausted      | `group <gid> generation <n>: cures exhausted (<k>/<max>); reporting only`                                                  |
| group done           | `group <gid>: completed`                                                                                                   |
| group failed         | `group <gid>: failed (<reason>)` / `terminal failed — … retry with: …`                                                     |
| run ended by you     | `run <id> aborted by operator: …`                                                                                          |

- **`not live for`** is evidence, not an alarm to act on by itself — see
  `triage-guide.md`, "When status reports Not Live", for the three cases and
  when manual intervention (`kill -INT -<pgid>`, `resume`) is actually
  warranted.
- **`machine suspend detected`** followed by **`suspend cure`** means the
  probe already killed and warm-resumed a child with no Sign of Life since
  the wake — no action needed unless `cures exhausted` follows.
- **`cures exhausted`** is the one line here that does ask you to act: the
  probe has used its budget for this generation and will only keep
  reporting from here.

Handy: `grep -E "verdict|ended \(|retired|coder launch|usage limit: (pausing|resuming)|not live for|live again|suspend" logs/run.log`.

## Phase 3 — Triage every escalation

The heart of the job. For each pending request, in order of how many groups
it blocks (the `blocks` clause on the raise line):

1. **Read the request JSON**: `prompt`, `kind`, `group_id`, `generation`, and
   `context` — `report_path`, `verdict_path`, `diff_summary`, `surprises`.
2. **Read the artifact it points at** (the coder report or reviewer verdict)
   and, when the report names commands or files, the worktree itself
   (`git -C <worktree> status`, the failing command's output).
3. **Classify the cause**, then act per the decision tree below. The full
   per-kind recipes, templates, and the "fix in worktree AND integration"
   procedure are in `triage-guide.md`.

| kind                                        | the cause is…                                             | action                                                                                                                                                               |
| ------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `coder_blocked` / `reviewer_too_hard`       | environment, deps, data, config, a hand-committable patch | fix it in the group's worktree **and** on the integration branch, then `--action retry`                                                                              |
| same                                        | spec ambiguity answerable from the plan / brainstorm docs | `--action answer --text …` (costs one rewrite — by design, the spec was wrong)                                                                                       |
| same                                        | a product / scope decision                                | `AskUserQuestion` with candidate answers, then `answer` with the human's words                                                                                       |
| same                                        | truly impossible in this run                              | `skip` with a note; `abort` if it invalidates the run                                                                                                                |
| `coder_question` (`needs_input`)            | answerable from docs                                      | `answer` — binding Operator Decision carried into every later prompt of the group; use `--guidance` only for advice that changes no scope, acceptance or deliverable |
| same                                        | not answerable                                            | ask the human, then `answer`                                                                                                                                         |
| `reviewer_structural`                       | the group boundaries are wrong                            | `answer` with a boundary decision — a rewrite is the right tool here                                                                                                 |
| `merge_conflict`                            | fixable by hand                                           | fix in the worktree, commit, `answer "resolved by hand: …"`; else `skip`                                                                                             |
| `preflight_failed`                          | a flake, or you fixed the world by hand (tree unchanged)  | `--action retry` with **no text**: re-runs the gate, no coder, no rewrite                                                                                            |
| same                                        | you changed a test/fixture the coder must know about      | `--action retry --text …`: fresh coder, same spec, your text as its note                                                                                             |
| same                                        | the diff is really wrong                                  | `answer` (rewrite); untracked leftovers are handled for you (relaunch, then archive)                                                                                 |
| `caps_exhausted`                            | visible progress in the diff                              | `answer` (grants one more generation/rewrite); no progress → `skip`                                                                                                  |
| `group_resolve`                             | a FAILED group's stranded work                            | inspect the worktree; commit what is salvageable; `answer`. **Never clean it.**                                                                                      |
| `respawn` / `group_start` / `merge_approve` | interactive tier only                                     | not raised at `on_stuck`; if seen, `answer` = proceed                                                                                                                |

Rules that override the table:

- **`retry` vs `answer`.** `retry` relaunches a fresh coder on the *same*
  spec with your text as an `## Operator note` — no rewrite budget, no speccer
  call. `answer` folds your text into a speccer rewrite (except
  `coder_question`, which warm-resumes). If you changed the world and not the
  spec, it is `retry`.
- **Systemic failure.** The same cause hitting a second group means the
  launch branch is wrong, not the group. `answer … --action abort`, fix on the
  launch branch, commit, `resume`. Write the finding down first.
- **Never answer blindly.** If you cannot classify the cause from the report,
  verdict, and worktree, that is the moment to ask the human — with what you
  found and two or three candidate answers, never open-ended.
- **Do not re-run an `answer` after "already answered".**

Log every escalation and its resolution (id, kind, group, cause, action,
text) to the notes file at the moment you answer it.

## Phase 4 — Finish

When the process exits (signal **(b)**):

1. `smart-mcps-orchestrate status $RUN` — per-group terminal states.
2. If groups failed: say which, why (the `failure:` line), and whether
   `smart-mcps-orchestrate retry $RUN <gid>` + `resume` is a sane salvage
   (it is, when the integration tip has since moved past the cause).
3. If every group completed/resolved, the CLI **auto-finished** (push + PR)
   the moment the last group went terminal — `run complete (N completed, M resolved by operator)` on stdout and the PR URL in `logs/run.log`. It
   prints `finish when ready with: smart-mcps-orchestrate finish $RUN` only
   in the not-finishable case (a group whose branch is not on the integration
   tip), and that is the only time you run `finish` by hand. The one-pager /
   report step below therefore happens **on the PR after the fact**: write it,
   then re-run `finish` to refresh the PR body. **The record the human
   approves from is the report, not this session's prose** — see
   `docs/orchestrator-report.md` for the full format contract. Before (re-)running
   `finish`:
   1. Check `.orchestrator/config.toml` `[docs] formats` — the report is only
      generated (and committed) when it names at least one format. If the
      repo wants a report and the block is empty or missing, add
      `[docs]\nformats = ["facts", "html", "changelog"]` yourself and commit
      it before launching the *next* run (`finish` reads it fresh, so this
      run only gets a report if it was already set before launch).
   2. Preview the computed formats now, so you write the one-pager from real
      facts: `smart-mcps-orchestrate report $RUN --format all --out /tmp/rr-$RUN`.
      This writes `facts.json`, `report.html`, and `CHANGELOG-entry.md`
      there and nothing else — it never touches `docs/RUNLOG.md` unless you
      add `--update-runlog`, and never writes into a worktree. `finish`
      re-renders the configured formats itself onto the integration branch.
   3. Write the one-pager — it IS the PR body and IS the Summary at the top
      of `report.html`, so it is the record the human approves from. Write it
      directly into the integration worktree, since that is where `finish`
      looks for it, and **before the last group merges**: the CLI
      auto-finishes the moment every group is terminal, and a one-pager
      written after that only lands if you run `finish` again to refresh the
      PR body.
      `smart-mcps-orchestrate report $RUN --out .worktrees/$RUN/integration/docs/runs/$RUN --scaffold one-pager`
      Expect the next merge to sweep that still-untracked scaffold into a
      `recover(<run>): integration work stranded by an interrupted run`
      commit — the merge cannot tell a driver's draft from a crashed group's
      leftovers. Harmless: `finish` overwrites the file with the filled-in
      one-pager and commits it under `docs/runs/$RUN/`.
      Then fill it in with the extract-then-abstract recipe:
      - **Extract.** Build one prompt from two XML-delimited sources and
        nothing else — never a transcript:
        ```
        <facts>…contents of /tmp/rr-$RUN/CHANGELOG-entry.md…</facts>
        <driver_notes>…contents of .orchestrator/notes-$RUN.md…</driver_notes>
        ```
        From them list `{pointer, fact quote}` items, one per thing worth
        saying, each pointer taken from the scaffold's
        `<!-- valid pointers: … -->` comment. If no pointer supports a
        statement, leave it out.
      - **Abstract.** Fill the four sections from that list only — TL;DR
        (exactly 3 bullets), Problems found (1–8), **Run notes** (1–8: what
        *you* did — hand fixes, the cause of each escalation, what was
        recovered; cite escalation ids and `gid/role/genN` session labels),
        Next steps (1–8). Each section may open with one plain paragraph of
        context (no pointer). Every top-level bullet ends in `(pointer)`; a
        bullet may carry indented continuation lines or ` -` sub-bullets,
        which need no pointer. Write Next steps as
        `- <action>: <why it matters and what "done" looks like> (pointer)`,
        optionally with a `  - how:` sub-bullet naming the first concrete
        move — a reader acts on these, so give each the context to act. No
        modal verbs in Problems found or Run notes; 900 words total over
        paragraphs, bullets and continuations.
      - **Verify.** Loop
        `smart-mcps-orchestrate report $RUN --validate .worktrees/$RUN/integration/docs/runs/$RUN/one-pager.md`
        until it exits 0, fixing **only** the bullets it names — a nonzero
        exit prints the exact rule that failed, never guess a fix. A present
        but invalid one-pager makes `finish` abort before it pushes, so do
        not skip the loop. Leaving `one-pager.md` absent is fine — `finish`
        generates the other formats without it and the PR body falls back
        to the run-record lines and the report link.
   4. **Run every `driver-run` verification item the run deferred to you.**
      `grep "driver-run verification item" logs/run.log` names them per group
      (the coder was told not to run them — a nested `claude` cannot write its
      transcript from inside a confined worktree). Run each from the group's
      worktree, or from the integration worktree once merged, and paste the
      result into the one-pager's Run notes. A live-tier item costs real
      tokens (`-m llm`, ~$0.20 and a few minutes here) — that is the price of
      the evidence, not a reason to skip it. A failure here is a finding: fix
      it and re-verify, or say plainly in the report that the item is unproven.
   5. Check what the run produced **outside git**. A group worktree's
      git-ignored files are copied into
      `.orchestrator/runs/$RUN/groups/<gid>/ignored-outputs/` before merge or
      `finish` removes it (environments and caches skipped; past the cap they
      are only named in `skipped.txt`; the `rescued N git-ignored file(s)`
      line is in `logs/run.log`), and untracked changes go into
      `leftover.patch`. Move anything the human needs from there, or from
      `git -C .worktrees/$RUN/integration status --ignored --porcelain`,
      somewhere durable.
   6. Run `smart-mcps-orchestrate finish $RUN` (again, to refresh the PR body)
      only after the human has seen the one-pager — it copies
      `.orchestrator/notes-$RUN.md` into the run dir as `driver-notes.md`,
      renders `[docs] formats` onto the integration branch (the one-pager
      folded into `report.html`), commits `docs/runs/$RUN/`, pushes, and opens
      or updates a PR whose body is the one-pager plus the run-record lines. To also record the run in `docs/RUNLOG.md`,
      run `report $RUN --format changelog --update-runlog` in the main
      checkout and commit it there. `smart-mcps-orchestrate export $RUN`
      writes the ingest bundle if the repo's workflow ingests runs.
4. Keep `.orchestrator/notes-<run_id>.md` as your own triage notes as you go
   (every escalation and how it was resolved, anything you fixed on the
   integration branch) — it is scratch for *you* and the raw material for the
   one-pager's Run notes, not the human-facing record. Do not duplicate the
   report's computed content there.
5. Surface anything the plan/deepen skills should learn — a verification item
   that turned out self-referential, a data path the plan named that
   `[workspace]` did not cover, a unit the grouper should have split — as a
   short list to the human. That feedback is the whole point of driving the
   run from a session that read the plan.

## Non-negotiable rules

- **Preflight is a gate, not a checklist.** Any red item blocks the launch.
- **HITL on, timeout set.** `--hitl --intensity on_stuck --escalation-timeout <s>` on every launch and resume from this skill; a run this session drives
  must never be able to wait forever on this session.
- **Detached, id known in advance.** `setsid` + `--run-id`; the process must
  outlive this session, and the notes file names it before it exists.
- **Event-driven watching.** `Monitor` on new request files / process exit /
  terminal log lines, plus a `status` fallback at most once an hour. No tight loops.
- **Fix, then `retry`; decide, then `answer`; ask only for product.**
  Environment, config, data, and plan defects are yours to fix. Scope and
  product trade-offs are the human's, asked with candidate answers.
- **Fixes land in both places.** A fix for a running group goes into its
  worktree (so it can continue) *and* onto `orchestrator/run-<id>` (so later
  groups fork clean).
- **Never clean a worktree.** Inspect, commit, cherry-pick — never `git clean`,
  never `reset --hard` on a group's branch.
- **Look at what changed before stopping work.** A running render or LLM call
  is not evidence of threshold-chasing; diff the artifacts it is producing
  first (r20260908: a render the driver interrupted was repairing a real
  defect).
- **Changes to how the output reads go to the human.** A worker commit that
  retunes a band, threshold or prompt shaping the product's texture is a
  decision to put to the human, even when it is inside the spec's letter.
- **Match processes by script path, never `pkill -f`/`pgrep -f` on a word.**
  The pattern matches this session's own shell (r20260913: exit 144 and a
  waiter that never returned); use `ps -eo pid,cmd | awk` on the full path.
- **Notes are written as you go**, to `.orchestrator/notes-<run_id>.md`.
