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
setsid nohup python -u -m orchestrator.cli run --repo "$(pwd)" --run-id $RUN \
  --hitl --intensity on_stuck --escalation-timeout 14400 \
  [--grouping NAME] \
  > .orchestrator/runs/$RUN/logs/driver.log 2>&1 < /dev/null &
echo $!
```

For a resume: the same, with `resume <run_id>` in place of `run … --run-id`.

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

Plus a slow heartbeat (20–30 min) that runs `smart-mcps-orchestrate status $RUN` and checks the driver line: `progressing (Ns since last heartbeat)` is
healthy; `heartbeat is stale` for more than one round length is a wedge;
`no process is driving this run` with unfinished groups means the process
died — inspect `driver.log`, then `resume`. A run paused on a usage limit
announces itself on the group heartbeats and reads as *paused*, not wedged —
wait it out.

Greppable anchors, all in `logs/run.log`:

| event                | line                                                                   |
| -------------------- | ---------------------------------------------------------------------- |
| escalation raised    | `ESCALATION <id> [<kind>] group <gid>` (+ `blocks …` if any)           |
| escalation answered  | `ESCALATION <id> answered: <action>`                                   |
| escalation timed out | `ESCALATION <id> timed out → <on_timeout>`                             |
| retry relaunch       | `group <gid> generation <n>: relaunching on the same spec …`           |
| spec rewrite         | `group <gid> generation <n>: rewriting spec (<why>) …`                 |
| group done           | `group <gid>: completed`                                               |
| group failed         | `group <gid>: failed (<reason>)` / `terminal failed — … retry with: …` |
| run ended by you     | `run <id> aborted by operator: …`                                      |

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

| kind                                        | the cause is…                                             | action                                                                                  |
| ------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `coder_blocked` / `reviewer_too_hard`       | environment, deps, data, config, a hand-committable patch | fix it in the group's worktree **and** on the integration branch, then `--action retry` |
| same                                        | spec ambiguity answerable from the plan / brainstorm docs | `--action answer --text …` (costs one rewrite — by design, the spec was wrong)          |
| same                                        | a product / scope decision                                | `AskUserQuestion` with candidate answers, then `answer` with the human's words          |
| same                                        | truly impossible in this run                              | `skip` with a note; `abort` if it invalidates the run                                   |
| `coder_question` (`needs_input`)            | answerable from docs                                      | `answer` — a warm resume; the text becomes the coder's next prompt verbatim             |
| same                                        | not answerable                                            | ask the human, then `answer`                                                            |
| `reviewer_structural`                       | the group boundaries are wrong                            | `answer` with a boundary decision — a rewrite is the right tool here                    |
| `merge_conflict` / `preflight_failed`       | fixable by hand                                           | fix in the worktree, commit, `answer "resolved by hand: …"`; else `skip`                |
| `caps_exhausted`                            | visible progress in the diff                              | `answer` (grants one more generation/rewrite); no progress → `skip`                     |
| `group_resolve`                             | a FAILED group's stranded work                            | inspect the worktree; commit what is salvageable; `answer`. **Never clean it.**         |
| `respawn` / `group_start` / `merge_approve` | interactive tier only                                     | not raised at `on_stuck`; if seen, `answer` = proceed                                   |

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
3. If every group completed/resolved, the CLI printed
   `finish when ready with: smart-mcps-orchestrate finish $RUN`. **The record
   the human approves from is the report, not this session's prose** — see
   `docs/orchestrator-report.md` for the full format contract. Before running
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
        (exactly 3 bullets), Problems found (1–5), **Run notes** (1–5: what
        *you* did — hand fixes, the cause of each escalation, what was
        recovered; cite escalation ids and `gid/role/genN` session labels),
        Next steps (1–5). Every bullet ends in `(pointer)`. No modal verbs in
        Problems found or Run notes; 450 words total.
      - **Verify.** Loop
        `smart-mcps-orchestrate report $RUN --validate .worktrees/$RUN/integration/docs/runs/$RUN/one-pager.md`
        until it exits 0, fixing **only** the bullets it names — a nonzero
        exit prints the exact rule that failed, never guess a fix. A present
        but invalid one-pager makes `finish` abort before it pushes, so do
        not skip the loop. Leaving `one-pager.md` absent is fine — `finish`
        generates the other formats without it and the PR body falls back
        to the run-record lines and the report link.
   4. Run `smart-mcps-orchestrate finish $RUN` only after the human has seen
      the one-pager — it copies `.orchestrator/notes-$RUN.md` into the run
      dir as `driver-notes.md`, renders `[docs] formats` onto the
      integration branch (the one-pager folded into `report.html`), commits
      `docs/runs/$RUN/`, pushes, and opens a PR whose body is the one-pager
      plus the run-record lines. To also record the run in `docs/RUNLOG.md`,
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
  terminal log lines, plus a 20–30 min `status` heartbeat. No tight loops.
- **Fix, then `retry`; decide, then `answer`; ask only for product.**
  Environment, config, data, and plan defects are yours to fix. Scope and
  product trade-offs are the human's, asked with candidate answers.
- **Fixes land in both places.** A fix for a running group goes into its
  worktree (so it can continue) *and* onto `orchestrator/run-<id>` (so later
  groups fork clean).
- **Never clean a worktree.** Inspect, commit, cherry-pick — never `git clean`,
  never `reset --hard` on a group's branch.
- **Notes are written as you go**, to `.orchestrator/notes-<run_id>.md`.
