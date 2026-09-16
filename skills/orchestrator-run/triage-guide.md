# Triage guide — resolving escalations as the run-driver session

Companion to `SKILL.md` Phase 3. One section per escalation kind: what to
read, what "good" looks like, and the exact command. Paths below assume
`RUN=<run id>`, `GID=<group id>`, `ESC=<escalation id>`:

| thing                    | where                                                                                                              |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| request / response files | `.orchestrator/runs/$RUN/escalations/request-$ESC.json`, `response-…`                                              |
| run log                  | `.orchestrator/runs/$RUN/logs/run.log`                                                                             |
| process stdout/stderr    | `.orchestrator/runs/$RUN/logs/driver.log` (written by the skill)                                                   |
| group artifacts          | `.orchestrator/runs/$RUN/groups/$GID/` (reports, verdicts, preflight)                                              |
| group worktree           | `.worktrees/$RUN/<gid>-<slug>/` (the report's `report_path` sits next to it; `git worktree list` is authoritative) |
| integration branch       | `orchestrator/run-$RUN`, checked out at `.worktrees/$RUN/integration`                                              |

The answer command, in every case:

```sh
smart-mcps-orchestrate answer $RUN $ESC --action <answer|retry|skip|abort> --text-file - <<'NOTE'
<why — backticks, quotes and $ survive verbatim>
NOTE
```

The text is required in spirit for `answer` and `retry` — it is what the next
coder reads. It is write-once: a second `answer` for the same id fails with
"already answered" and must not be retried. So:

- **Any note with a backtick, `$` or quote goes through `--text-file`** (a
  quoted heredoc as above, or a file). A double-quoted `--text` is
  command-substituted by the shell before the CLI sees it — r20260907 lost
  six backticked terms from a note that way. Short plain text may use `--text`.
- **Check the echo**: `answered <id>: <action> (N chars recorded)`. If N is
  far below what you wrote, `cat escalations/response-$ESC.json` and tell the
  human; the answer cannot be corrected.

## The two resolutions that cost nothing vs the one that costs a rewrite

- **`retry`** — the cause was *outside the worker*: environment, dependency
  spec, data layout, config, a patch you committed by hand. A fresh coder is
  launched on the **unchanged spec** and its prompt ends with
  `## Operator note` + your text. No `rewrite_spec`, no speccer call, no
  `rewrites` increment. The `run.log` line is
  `group $GID generation N: relaunching on the same spec (operator retry: …)`.
- **`answer` on `coder_question`** — a warm resume of the same coder session;
  your text is its next prompt, verbatim. Free.
- **`answer` on anything else** — folded into a speccer rewrite as an
  `[operator]` surprise. Costs one rewrite of `max_rewrites` and one speccer
  call. Correct when the *spec* was the problem; wasteful when the world was.

If you find yourself typing "I fixed X, go again" after `--action answer`,
stop — that is a `retry`.

## Fix in the worktree AND on the integration branch

A fix that only lands in the group's worktree lets that group continue but
every later group forks from `orchestrator/run-$RUN` and hits the same wall.
A fix that only lands on the integration branch does not reach the group
already running. Do both, in this order:

```sh
WT=$(git worktree list | awk -v g="$RUN/$GID" '$0 ~ g {print $1}')
# 1. fix inside the group's worktree and commit there
$EDITOR "$WT/pyproject.toml"            # or whatever the cause is
git -C "$WT" add -A && git -C "$WT" commit -m "fix(env): <what> (operator, run $RUN)"
FIX=$(git -C "$WT" rev-parse HEAD)
# 2. carry the same commit onto the integration branch
INT=.worktrees/$RUN/integration
git -C "$INT" cherry-pick "$FIX"
# 3. if the fix changes the venv, rebuild it where the group runs
(cd "$WT" && uv sync <provision_args>)
# 4. now relaunch
smart-mcps-orchestrate answer $RUN $ESC --action retry --text "fixed: <what>, committed $FIX in the worktree and cherry-picked to orchestrator/run-$RUN"
```

Never `git clean -fd`, `git checkout -- .`, or `git reset --hard` in a group's
worktree. A crashed or blocked group's uncommitted files are its progress;
the 2026-07-26 recovery lesson (memory: *orchestrator recovery + cleaning
lesson*) is that cleaning them cost a whole group's work. If the tree is
messy, `git -C "$WT" stash` or commit it as `wip:` — both are reversible.

## Per kind

### `coder_blocked` (also a downgraded `needs_input` under `orchestrator_only`)

Read: `context.report_path` (the coder's JSON report — `summary`, `blocked_on`,
`surprises`, `verification` items), then `context.diff_summary`, then the
worktree if the report names a command (`uv run pytest …`, a build) — run it
yourself in `$WT` and read the real stderr.

Good looks like: you can name the cause in one sentence and say whether it is
in the world or in the spec.

| cause                                                                             | action                                                                                      |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| venv did not build, extra missing, lockfile stale                                 | fix `pyproject`/`uv.lock` in both places → `retry`                                          |
| data file / dir not where the spec says                                           | fix `[workspace] data_dirs` or move the data (outside git) → `retry --text "data now at …"` |
| a tool/CLI missing on this machine                                                | install it → `retry`                                                                        |
| the spec asks for something the codebase contradicts (wrong path, renamed symbol) | `answer --text "<the correct fact>"` (rewrite is right: the spec must change)               |
| the spec needs a product decision                                                 | `AskUserQuestion` → `answer` with the human's words                                         |
| cannot be done in this run                                                        | `skip --text "<why>"`; `abort` if every remaining group depends on it                       |

### `reviewer_too_hard`

Read: `context.verdict_path` (the reviewer's verdict — `notes`,
`required_changes`), the diff summary, the coder's latest report beside it.

`too_hard` usually means the reviewer could not *verify*, not that the coder
failed: a verification item that needs data, a service, or a tool the
reviewer's sandbox lacks. If the environment is the reason → fix in both
places → `retry`. If the item is genuinely unverifiable as written (the plan
asked for "a real model rendering real audio" and no model is available) →
that is a plan defect: `answer --text "verify by … instead"` **and** note it
for the plan/deepen feedback in Phase 4.

### `reviewer_structural`

Read: the verdict. The reviewer thinks the group boundary is wrong — a change
that needs a file another group owns, or a unit that should not be here.
The rewrite *is* the right tool: `answer --text "<boundary decision>"`, e.g.
"leave `api/routes.py` to g3; implement only the model change and expose a
hook g3 will call". If the boundary change is large enough that the grouping
is wrong, `abort`, regroup (`smart-mcps-orchestrate group <plan>`), and start
a new run — say so to the human first.

### `coder_question` (`needs_input`)

Read: the report's `question` and `summary`. Answer from the plan, the
brainstorm doc, `STRATEGY.md`, or the code — the text you give is the coder's
next prompt, so write it as an instruction, not a musing:

```sh
smart-mcps-orchestrate answer $RUN $ESC --action answer --text "Use the LRU cache from utils/cache.py; do not add a new dependency."
```

Ask the human only when the question is a product choice, and hand them 2–3
concrete candidates.

**`answer` binds by default.** Every `coder_question` answer becomes an
Operator Decision, carried verbatim into every later coder, handoff,
reviewer, re-review and rewrite-speccer prompt of the group — a later
generation's reviewer treats it as a spec amendment, not something to second-
guess as "self-invented". Use `--guidance` only when the text is advice that
changes no scope, acceptance, or deliverable (a style nudge, a pointer to a
file) — a `--guidance` answer is not carried forward the same way, so never
use it for anything you would mind a later generation forgetting. Write
every binding decision in the notes file the moment it is made.

### `merge_conflict`

Read: the `surprises` on the request (which groups collide on which files),
then `git -C "$WT" status` and `git -C "$WT" diff` for the conflict markers
after the failed in-place resolve. Resolve by hand in the worktree, run the
group's own verification, commit, then:

```sh
smart-mcps-orchestrate answer $RUN $ESC --action answer --text "resolved by hand in the worktree (commit $FIX); merge again"
```

(`retry` behaves identically here — a conflict is evidence about the diff, so
the orchestrator rewrites either way.) If the conflict is real and large,
`skip` the later group and let `group_resolve` handle its stranded work.

### `preflight_failed`

Read: `.orchestrator/runs/$RUN/groups/$GID/preflight-check*.log` and the
junit XML beside it. The merge gate ran the repo's check command on the
merged result and it failed. Two cases:

- **A test the diff broke** → `answer --text "<what to fix>"` (rewrite).
- **A test that was already red on the launch branch** → the CLI normally
  classifies this as unattributable and does not escalate; if you see it
  anyway, the baseline gate is the problem — fix or quarantine the test on the
  launch branch, `abort`, `resume`.

### `caps_exhausted`

Read: the diff summary and the last report. Is there visible, converging
progress (fewer failing tests, the reviewer's `required_changes` shrinking)?
`answer --text "<what to focus on>"` grants exactly one more generation or
rewrite. Otherwise `skip` — the budget existed for a reason.

### `group_resolve`

A FAILED group's branch holds stranded commits its dependents need. Read:
`git -C "$WT" log --oneline orchestrator/run-$RUN..HEAD` and
`git -C "$WT" status`. Inspect, commit what is salvageable, and `answer` —
the scheduler then merges what it can and verifies containment itself. Or
merge by hand into `orchestrator/run-$RUN`, verify, and `answer --text "merged by hand"`. `skip` leaves the group failed and its dependents stranded.

### `respawn` / `group_start` / `merge_approve`

Interactive-tier approval gates — not raised under `on_stuck`. If a run was
launched at `interactive`, `answer` means proceed, `skip` fails the group,
`abort` stops the run.

## When it is systemic

The same cause on a second group (two `coder_blocked` on "no module named
X"; two `reviewer_too_hard` on the same unverifiable item) means the launch
branch or the plan is wrong. Do not answer group by group:

1. Write the finding to `.orchestrator/notes-$RUN.md`.
2. `smart-mcps-orchestrate answer $RUN $ESC --action abort --text "<systemic cause>"`.
3. Fix on the launch branch (and on `orchestrator/run-$RUN` — a `resume` forks
   later groups from the integration tip, not from the launch commit).
4. `smart-mcps-orchestrate resume $RUN …` with the same HITL flags, detached.

## When status reports Not Live

`status` derives its liveness line from the same facts as the `not live for`
and `live again` lines in `logs/run.log` (plan U2/U3). Three cases:

- **Live again on its own.** A `not live for …` line followed later by
  `live again: …` with no action from you — the child was slow, not dead.
  Nothing to do.
- **Not live after a machine suspend, with Suspend Cures left.** A
  `machine suspend detected: …` line precedes it and `status` (or
  `cures exhausted`, see below) shows cures remaining this generation — the
  probe will cure the child itself (kill and warm-resume in place). Wait for
  the `suspend cure <k>/<max> — …` line rather than intervening.
- **Not live with cures exhausted, or no suspend detected at all.** `status`
  prints the `cures exhausted (<k>/<max> this generation) — kill -INT -<pgid> then smart-mcps-orchestrate resume <run_id>` line, or there was never a
  `machine suspend detected` line to explain the silence. This is the one
  case left that needs your hand: `kill -INT -<pgid>`, then `resume`.

## When a terminal `failed` line appears without an escalation

`group $GID: terminal failed — branch …, worktree …, retry with: smart-mcps-orchestrate retry $RUN $GID` means the group hit its re-entry
cap or `on_group_failure=halt` classed it terminal. Resumes after you stopped
the driver with `kill -INT`/`-TERM` do not count toward that cap — only
resumes after a crash or reboot do. `status` keeps showing the old failure
line after `retry` until the next `resume` picks the group up. That `retry` is the
**subcommand** (release the group), not the escalation action. Run it only
after the cause is fixed on the integration branch, then `resume`.
