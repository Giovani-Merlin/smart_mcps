# smart-mcps-orchestrate

Multi-agent orchestrator: takes an implementation plan, groups its tasks by code
locality (codegraph-backed), and executes the groups as parallel `claude` CLI
sessions with paired review, a circuit breaker, and per-run integration-branch
merges. Everything runs through the `claude` CLI on your subscription — there is
no API client anywhere, and the orchestrator itself spends zero tokens.

## Commands

```sh
smart-mcps-orchestrate group <plan.md> [--repo DIR] [--name NAME] [--dry-run] [--token-budget N]
smart-mcps-orchestrate run   [--repo DIR] [--run-id ID] [--grouping NAME] [--sequential]
                             [--concurrency N] [--permission-mode MODE]
                             [--review-intensity TIER] [--hitl] [--intensity TIER]
                             [--escalation-source SRC] [--escalation-timeout S]
smart-mcps-orchestrate groupings [--repo DIR]
smart-mcps-orchestrate status [RUN_ID] [--repo DIR]
smart-mcps-orchestrate resume RUN_ID [--repo DIR] [...same execution flags as run]
smart-mcps-orchestrate answer RUN_ID ESC_ID [--action answer|skip|abort] [--text ...] [--repo DIR]
```

- **`group`** — LLM-maps plan tasks to code regions, partitions them into
  execution groups, and writes a named grouping directory:
  `.orchestrator/groupings/<name>/groups.json` +
  `.../base-context.md`. `--name` defaults to the plan's filename stem, so
  grouping different plans never overwrites each other. `--dry-run` prints the
  groups, DAG, and token estimates without writing anything — the human
  checkpoint before execution.
- **`run`** — selects a grouping (`--grouping NAME`, or auto-selects when exactly
  one exists; with several present, or only a pre-named-grouping legacy
  artifact, it lists what's there and exits rather than guessing), snapshots it
  into the run directory, starts one base session holding the compiled base
  context, forks a coder (and, per review tier, a reviewer) session per group in
  its own worktree, schedules groups in dependency order, and merges approved
  groups into the run's integration branch `orchestrator/run-<run_id>`. Exit 0
  only if every group completed. The final merge of the integration branch into
  your main branch is deliberately manual.
- **`groupings`** — lists every named grouping with its plan path and group
  count.
- **`status`** — lists runs, or pretty-prints one run's per-group state,
  generations, failures, and sessions.
- **`resume`** — re-enters a crashed or interrupted run: terminates orphaned
  worker processes, restarts mid-flight groups from ready, reuses the original
  base session, and never relaunches completed groups. A `failed` group is
  terminal; to retry one after fixing the cause, edit its entry in the run's
  `state.json` back to `"ready"` and resume.
- **`answer`** — resolves a pending human-in-the-loop escalation (see below) by
  writing its response file: `--action answer` (with `--text` guidance) resumes
  or guides the blocked group, `--action skip` fails it, `--action abort` stops
  the run. The blocked group's coroutine picks the answer up by correlation id.

## Human-in-the-loop (HITL)

By default a `run` has HITL escalation **on**, at the `on_stuck` intensity tier:
every hard moment (a coder reporting `blocked`, a reviewer `too_hard`/`structural`,
a merge conflict, an exhausted generation/rewrite cap) pauses and escalates to
**you** instead of auto-resolving. Pass `--hitl` to be explicit about this (it's
redundant against the default but documents intent), or `--intensity autonomous`
to run unattended and auto-resolve/fail everything instead.

Because headless `claude -p` workers cannot pause mid-turn to ask a question, the
only channel is **report-then-resume**: a coder that needs a human decision ends
its turn with `status: needs_input` + a `question`; the orchestrator escalates,
blocks that group, and resumes it with your answer — sibling groups keep running.

**How you drive it.** Launch `run --hitl` in the background with its output going
to a log you tail; watch `escalations/request-*.json` for a request with no
matching `response-*.json`; answer with `smart-mcps-orchestrate answer <run_id> <esc_id> ...` (or `status <run_id>`, which lists pending escalations). The
orchestrator process stays alive the whole time — a pause is just an `await`.

**Intensity tiers** (`--intensity`, or `[escalation] intensity`):

| Tier          | Escalates                                                                                                         |
| ------------- | ----------------------------------------------------------------------------------------------------------------- |
| `autonomous`  | nothing — run unattended (pass `--intensity autonomous` explicitly to get this)                                   |
| `on_failure`  | only a generation/rewrite cap about to fail a group                                                               |
| `on_stuck`    | *(the run default)* coder `blocked`/`needs_input`, reviewer `too_hard`/`structural`, merge conflict, terminal cap |
| `interactive` | additionally approve before group launch, before each respawn, and before each merge                              |

Routine `changes_required` review rounds and routine breaker respawns stay
autonomous under `on_stuck` — only genuinely stuck moments pause.

**Escalation source** (`--escalation-source`): `workers_via_orchestrator`
(default) surfaces a coder's `needs_input` question to you; `orchestrator_only`
downgrades it to a blocked-style rewrite (no direct worker→human channel).

**Unanswered escalations** block indefinitely by default (a live operator is
expected). Set `--escalation-timeout <s>` (or `[escalation] timeout_s`) to fall
back after a wait — per `[escalation] on_timeout` = `autonomous` | `skip` |
`abort`.

HITL is **on by default** (`enabled = true`, `on_stuck`) — an unattended run
needs `--intensity autonomous` (or `[escalation] intensity = "autonomous"` in
config) to opt back out, otherwise it can block indefinitely on an unanswered
escalation. `--hitl` alone is a no-op against the default; it only matters
when a config file has turned escalation off.

## Configuration

`.orchestrator/config.toml` in the target repo (or `--config PATH`). Resolution
is **CLI flags > config file > defaults**; every field has a working default.

```toml
[execution]
concurrency = 1                # parallel groups (--concurrency)
sequential = false            # one-at-a-time debug mode (--sequential)
permission_mode = "acceptEdits"  # claude CLI permission mode (--permission-mode)
max_rewrites = 2              # spec rewrites per group before it fails
max_conflict_resolve_attempts = 1  # warm-resume attempts to resolve a merge conflict before rewriting

[breaker]                     # circuit breaker per coder session
context_token_limit = 200000  # latest-round context tokens before retirement
max_rounds_per_generation = 3
max_generations = 3           # respawns before the group fails to the operator

[estimator]
token_budget = 100000         # per-group budget for partitioning (--token-budget)

[session]
claude_bin = "claude"         # or a list, e.g. ["python", "tests/fake_claude.py"]
model = ""                    # optional --model for worker sessions
allowed_tools = []            # optional --allowedTools list
transcript_root = ""          # default: ~/.claude/projects

[difficulty]                  # review-tier thresholds
d_review = 0.35               # below: self_verify (no reviewer session)
d_hard = 0.65                 # above: paired_plus (mandatory extra pass)

[escalation]                  # human-in-the-loop (on by default)
enabled = true                 # false to run unattended, or pass --intensity autonomous
intensity = "on_stuck"        # autonomous | on_failure | on_stuck | interactive
source = "workers_via_orchestrator"  # or orchestrator_only
# timeout_s = 30.0            # omit to block indefinitely; else on_timeout fires
on_timeout = "autonomous"     # autonomous | skip | abort (when timeout_s is set)
poll_interval_s = 1.0         # response-file poll cadence
```

`--review-intensity self_verify|paired|paired_plus` overrides the computed tier
for **every** group in the run.

## Run artifacts

All run state lives in the target repo, never under `~/.claude`:

```
<repo>/
  .orchestrator/
    config.toml               # optional; yours
    groupings/<name>/
      groups.json              # grouping output (the run's input)
      base-context.md          # compiled shared context for the base session
    failures/                 # raw LLM output that failed validation
    runs/<run_id>/
      groups.json              # snapshot of the grouping the run started with
      base-context.md          # snapshot of the base context at run start
      manifest.json           # run → groups → sessions join (the analyzer contract)
      state.json              # crash-resumable scheduler state + live worker PIDs
      groups/<gid>/           # report-g<G>-r<R>.json / verdict-g<G>-r<R>.json
      logs/run.log            # HITL event log — the live log the main session tails
      escalations/            # HITL request-<id>.json / response-<id>.json (correlation ids)
  .worktrees/
    run-<run_id>-integration/ # the integration branch's worktree
    <gid>-<name>/             # per-group worktrees (removed after a clean merge)
```

A run never reads the live `groupings/<name>/` directory again after it starts:
it works from its own snapshot, so a later `group --name <same>` against a
different plan cannot rewrite a finished or in-flight run's history.

Branches: each group works on `orchestrator/<run_id>-<gid>`; approved groups
merge `--no-ff` into `orchestrator/run-<run_id>` (one merge commit per group).

## .gitignore the run artifacts

Add to the **target repo's** `.gitignore` before the first run, so run state and
worktrees never land in commits:

```gitignore
.orchestrator/
.worktrees/
```

## Self-modifying plans take effect on the next run

The orchestrator drives itself from the **installed** console script, while its
workers edit source in isolated worktrees that are never on the running
interpreter's path. **Worker changes to `orchestrator/` therefore take effect
on the next run — after merge and reinstall — never the run that makes them.**
A plan that changes the CLI or scheduler and expects the same run to exercise
the change is mis-sequenced. `group` prints a warning when a plan's mappings
touch paths under `orchestrator/`, so this is surfaced at grouping time rather
than discovered mid-run.

## Testing

The entire suite runs offline against `tests/fake_claude.py`, a scripted stub
that speaks the real CLI surface (fork/resume/session-id/JSON envelopes) and can
play per-session scripts, write files, and create real git commits in worker
worktrees. See `tests/test_e2e_stub.py` for full-run scenarios: happy path,
review rejection, breaker trip, surprise-driven rewrite, merge conflict, and
resume.
