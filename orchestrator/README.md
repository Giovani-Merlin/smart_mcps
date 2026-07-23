# smart-mcps-orchestrate

Multi-agent orchestrator: takes an implementation plan, groups its tasks by code
locality (codegraph-backed), and executes the groups as parallel `claude` CLI
sessions with paired review, a circuit breaker, and per-run integration-branch
merges. Everything runs through the `claude` CLI on your subscription — there is
no API client anywhere, and the orchestrator itself spends zero tokens.

## Commands

```sh
smart-mcps-orchestrate group <plan.md> [--repo DIR] [--dry-run] [--token-budget N]
smart-mcps-orchestrate run   [--repo DIR] [--run-id ID] [--sequential] [--concurrency N]
                             [--permission-mode MODE] [--review-intensity TIER]
                             [--hitl] [--intensity TIER] [--escalation-source SRC]
                             [--escalation-timeout S]
smart-mcps-orchestrate status [RUN_ID] [--repo DIR]
smart-mcps-orchestrate resume RUN_ID [--repo DIR] [...same execution flags as run]
smart-mcps-orchestrate answer RUN_ID ESC_ID [--action answer|skip|abort] [--text ...] [--repo DIR]
```

- **`group`** — LLM-maps plan tasks to code regions, partitions them into
  execution groups, and writes `.orchestrator/groups.json` +
  `.orchestrator/base-context.md`. `--dry-run` prints the groups, DAG, and token
  estimates without writing anything — the human checkpoint before execution.
- **`run`** — consumes the `group` artifacts: starts one base session holding the
  compiled base context, forks a coder (and, per review tier, a reviewer) session
  per group in its own worktree, schedules groups in dependency order, and merges
  approved groups into the run's integration branch `orchestrator/run-<run_id>`.
  Exit 0 only if every group completed. The final merge of the integration branch
  into your main branch is deliberately manual.
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

By default a `run` is fully autonomous — every hard moment (a coder reporting
`blocked`, a reviewer `too_hard`/`structural`, a merge conflict, an exhausted
generation/rewrite cap) is auto-resolved or, at the terminal cap, marked
`failed`. `--hitl` opts the run into escalating those moments to **you** instead.

Because headless `claude -p` workers cannot pause mid-turn to ask a question, the
only channel is **report-then-resume**: a coder that needs a human decision ends
its turn with `status: needs_input` + a `question`; the orchestrator escalates,
blocks that group, and resumes it with your answer — sibling groups keep running.

**How you drive it.** Launch `run --hitl` in the background with its output going
to a log you tail; watch `escalations/request-*.json` for a request with no
matching `response-*.json`; answer with `smart-mcps-orchestrate answer <run_id> <esc_id> ...` (or `status <run_id>`, which lists pending escalations). The
orchestrator process stays alive the whole time — a pause is just an `await`.

**Intensity tiers** (`--intensity`, or `[escalation] intensity`):

| Tier          | Escalates                                                                                                              |
| ------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `autonomous`  | nothing (identical to a no-`--hitl` run)                                                                               |
| `on_failure`  | only a generation/rewrite cap about to fail a group                                                                    |
| `on_stuck`    | *(the `--hitl` default)* coder `blocked`/`needs_input`, reviewer `too_hard`/`structural`, merge conflict, terminal cap |
| `interactive` | additionally approve before group launch, before each respawn, and before each merge                                   |

Routine `changes_required` review rounds and routine breaker respawns stay
autonomous under `on_stuck` — only genuinely stuck moments pause.

**Escalation source** (`--escalation-source`): `workers_via_orchestrator`
(default) surfaces a coder's `needs_input` question to you; `orchestrator_only`
downgrades it to a blocked-style rewrite (no direct worker→human channel).

**Unanswered escalations** block indefinitely by default (a live operator is
expected). Set `--escalation-timeout <s>` (or `[escalation] timeout_s`) to fall
back after a wait — per `[escalation] on_timeout` = `autonomous` | `skip` |
`abort`.

`--hitl` is opt-in: with no flag the CLI stays autonomous and unattended runs
never hang, so `--intensity autonomous` and a plain `run` behave identically.

## Configuration

`.orchestrator/config.toml` in the target repo (or `--config PATH`). Resolution
is **CLI flags > config file > defaults**; every field has a working default.

```toml
[execution]
concurrency = 3               # parallel groups (--concurrency)
sequential = false            # one-at-a-time debug mode (--sequential)
permission_mode = "acceptEdits"  # claude CLI permission mode (--permission-mode)
max_rewrites = 2              # spec rewrites per group before it fails

[breaker]                     # circuit breaker per coder session
context_token_limit = 120000  # latest-round context tokens before retirement
max_rounds_per_generation = 3
max_generations = 3           # respawns before the group fails to the operator

[estimator]
token_budget = 100000         # per-group budget for partitioning (--token-budget)

[session]
claude_bin = "claude"         # or a list, e.g. ["python", "tests/fake_claude.py"]
timeout_s = 1800.0            # per-round subprocess timeout
model = ""                    # optional --model for worker sessions
allowed_tools = []            # optional --allowedTools list
transcript_root = ""          # default: ~/.claude/projects

[difficulty]                  # review-tier thresholds
d_review = 0.35               # below: self_verify (no reviewer session)
d_hard = 0.65                 # above: paired_plus (mandatory extra pass)

[escalation]                  # human-in-the-loop (off by default)
enabled = false               # true, or pass --hitl
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
    groups.json               # grouping output (the run's input)
    base-context.md           # compiled shared context for the base session
    failures/                 # raw LLM output that failed validation
    runs/<run_id>/
      manifest.json           # run → groups → sessions join (the analyzer contract)
      state.json              # crash-resumable scheduler state + live worker PIDs
      groups/<gid>/           # report-g<G>-r<R>.json / verdict-g<G>-r<R>.json
      logs/run.log            # HITL event log — the live log the main session tails
      escalations/            # HITL request-<id>.json / response-<id>.json (correlation ids)
  .worktrees/
    run-<run_id>-integration/ # the integration branch's worktree
    <gid>-<name>/             # per-group worktrees (removed after a clean merge)
```

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
