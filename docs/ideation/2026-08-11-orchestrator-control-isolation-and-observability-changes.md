# Principal changes: per-turn control, worker isolation, and live observability

Date: 2026-08-11
Branch: `main` (at `34ad138`, after run `obsprov1` merged)
Evidence: `.orchestrator/notes-observatory-provenance.md` (13 findings, live run `obsprov1`) and
`.orchestrator/research-2026-08-11-isolation-and-observability.md` (external research digest)

This document states **what will change and why**, ahead of the implementation plan. Everything here
is grounded in a real 9-group run that hit five session-limit stops, one terminal failure, one
18-hour silent stall, and one false write into the operator's global memory.

______________________________________________________________________

## The single structural change everything else depends on

**Today a "round" is one `claude -p` invocation, and the orchestrator is blind and mute inside it.**

`SessionRunner._call` blocks on a single `subprocess.communicate()` (`execution/sessions.py:319`).
Follow-up messages are sent by `nudge_until_report` calling `runner.resume(...)`
(`execution/sessions.py:252`), which spawns a **new** `claude -p --resume <sid>` process. So the only
moment the orchestrator can observe or influence a session is *between* rounds.

That is the root cause of three separate symptoms that looked unrelated:

| symptom                                                  | why                                                       |
| -------------------------------------------------------- | --------------------------------------------------------- |
| `rounds 0, started_at None, ctx 0` while 4 commits exist | usage can only be recorded after `communicate()` returns  |
| one round reached **306,157** tokens                     | nothing can check a budget mid-round                      |
| escalation invisible for 18h                             | no channel exists to interject while a round is in flight |

Measured, per session, on run `obsprov1` group g3:

```
coder    91d7b1f5  295 turns  peak single-turn prompt = 306,157   manifest 310,260
coder    d5ab2688  121 turns  peak =  99,152                      manifest 103,276
reviewer 97ef786c   77 turns  peak =  89,059                      manifest  91,509
```

One invocation, 295 internal turns, 306k tokens, `rounds_completed: 1`. A between-rounds check would
not have caught any of it.

### Change 1 — move the worker channel to bidirectional streaming

`--output-format stream-json --include-partial-messages` for output, `--input-format stream-json`
("realtime streaming input", confirmed present on the installed CLI) for input.

This buys **per-turn observation and per-turn control**: the orchestrator can see each turn's usage
as it happens and can send a message into a running session — exactly the capability an interactive
conversation has, and which the orchestrator currently lacks.

Blast radius, stated honestly: `_call`/`_spawn` change from a blocking `communicate()` to an
incremental reader, and `RoundResult` / `RoundUsage.from_envelope` (`sessions.py:85`) currently
assume a single final envelope carrying `usage.iterations`. Every consumer of `RoundResult` is
downstream of that assumption.

**Transcript tailing is kept, but for two different jobs**: sessions we did not spawn (the grouper's
mapper/speccer calls), and post-mortem forensics — the 306k figure above was reconstructed from
transcripts *after* the fact. It is deliberately not the live channel, because it is read-only,
its schema is undocumented, and it reads from `~/.claude`, which Change 2 locks down.

______________________________________________________________________

## Change 2 — confine worker writes with Landlock

**What happened:** a worker running under `permission_mode = "acceptEdits"` — which accepts
`Edit`/`Write` at *any* filesystem path — reached outside its worktree and edited
`~/.claude/projects/-home-gbm1996-wksp-smart-mcps/memory/`, writing a claim that was **factually
false** (that grouper transcripts were not being captured; its own artifacts disproved it). A
previous instance of the same bug in July happened to write a true claim, so the "it is usually
right" defence is no longer available.

**Why permission rules alone are not enough:** a `PreToolUse` hook only sees what routes through a
tool it can inspect. It cannot see `bash -c 'cat > ~/x'`, `python -c 'open(...,"w")'`, an editor, a
language server, or git writing refs. Deny-rules are a guard-rail against a well-behaved agent's
mistake — which *is* the observed failure mode — not a boundary.

**Why not containers:** Landlock (kernel ≥5.13; this host runs 6.6) is unprivileged, applied in
`preexec_fn` before `exec`, and **inherited by every child process** — bash, python, git, editors.
It is a true kernel-enforced boundary without image builds, volume plumbing, or slower spawns.

**The carve-out shape.** Landlock is **allowlist-only**: a rule on a parent grants the entire
subtree and there is no subtraction, so "allow `~/.claude`, deny `projects/*/memory/`" is
inexpressible. It must be enumerated. Fortunately the structure makes this clean — memory dirs live
at `~/.claude/projects/<slug>/memory/` under **operator** project slugs, while a worker's transcripts
go to `~/.claude/projects/<worktree-slug>/`, and **no worker slug dir has a `memory/` subdir**:

```
allow rw : <worktree>
allow rw : ~/.claude/projects/<this worker's own slug>/     # its own transcript only
allow r  : ~/.claude/.credentials.json, settings.json, plugins/, skills/
allow rw : ~/.claude/shell-snapshots/                       # the Bash tool sources these
allow rw : (runtime dirs — cache/, session-env/, sessions/, file-history/, tasks/)
deny     : everything else, including every other projects/<slug>/ and therefore ALL memory dirs
```

The operator's memory is not specially denied — it is simply never allowed. A worker cannot write
memory for its own project either, since its slug dir has no `memory/`.

**Deliberately empirical:** the exact runtime-dir list is to be *measured* by running one worker
under the ruleset and observing what breaks, not guessed.

**Layered with, not replaced by, permission rules.** Per-worker `--settings` / `--disallowedTools`
(both present on the CLI, neither currently used — `sessions.py:274-288` passes only
`--permission-mode` and optionally `--allowedTools`) still ship, because they give clearer errors for
the common accidental case. `SessionRunner` is the only module that shells the CLI, so the entire
confinement surface is one file.

______________________________________________________________________

## Change 3 — a staged context budget, enforced mid-round

**Decision:** the ladder is measured against the existing `[breaker] context_token_limit` (200,000).

```
 70%  ->  140,000   ask for a progress summary
 90%  ->  180,000   ask for prioritised conclusions
100%  ->  200,000   ask for a compact report, then force-stop after it lands
```

Semantics, as specified by the operator: a round is **not** interrupted for being at 99%. The ladder
fires on crossing, and a round already past 100% is asked for the compact report and stopped once it
delivers. Overshoot is accepted.

The staged design is deliberate. The known failure mode of a bare "warn then force a report" is that
**the wrap-up turn is itself expensive**, so the forced report either truncates or blows the budget
further. Because the 100% report can reference the summaries produced at 70% and 90%, it is cheap.
Headroom exists in practice: a single request has already carried 306k, so the effective window is
larger than 200k and the wrap-up has room.

**This requires Change 1.** Without a live feed there is no way to observe 70%, and without
streaming input there is no way to say anything to a running round.

**Not a contradiction of R7.** `_spawn` deliberately has no wall-clock timeout — "wall-clock is a
terrible proxy for stuck" (`sessions.py:322-324`). A *token* threshold is a proxy for **cost**, not
for stuck. This will be stated in the code so a later reader does not "fix" it.

**Note:** there is no `--max-turns` flag on the installed CLI, so any turn ceiling must be enforced
externally by counting and killing.

______________________________________________________________________

## Change 4 — make a blocked run loud

**What happened:** run `obsprov1` sat idle for **~18 hours** on one unanswered `group_resolve`
escalation. A single failed group also blocked an unrelated pending group through declared-file
overlap.

**Correction to a first reading:** the escalation was **not** unlogged. `EscalationBroker.raise_escalation`
(`execution/escalation.py:97-103`) already writes `request-<id>.json` **and** calls `log_event`, and
`run.log:67` carries the full line. What actually happened is narrower: the line goes to `run.log`
only, never to **stdout**, and `run.log`'s mtime then froze *because that line was the last thing
written before the block*. An operator (or monitor) watching the process's output sees an alive
process and total silence.

So the fix is smaller than "add logging" — the log line exists and is good.

**Decision:** notify loudly, but **keep blocking indefinitely** — a human always decides, the run
simply stops being silent about waiting. No automatic timeout fallback.

- print the escalation prompt to stdout and `run.log` when raised (the `_log` path in `review.py`
  already exists)
- surface pending escalations in the Observatory
- state which groups are blocked *because of* it

______________________________________________________________________

## Change 5 — stop workers touching repo-global git state

**What happened:** a worker ran `git stash -q -u` to get a clean tree for a lint check. **The stash
stack is repo-global and shared by every worktree.** This collided with a two-month-old operator
stash on `main`, resurrecting `agentmemory/cli.py` — a file legitimately deleted long ago — which then
made `_refresh_onto_tip`'s merge conflict and killed group g1.

**Decision: deny the shared-stack commands to workers.** A worker owns a private branch; a throwaway
WIP commit does everything stash does and is branch-local.

```
deny to workers: git stash | git reset --hard | git clean | git gc | git worktree prune
```

`git gc` is included on research advice: auto-gc can fire from *any* worktree and contend on lock
files across the shared object DB while N agents commit. It should run centrally when idle.

**A private per-worktree stash was considered and deferred.** It is documented-correct — the
`git worktree` man page confirms `refs/worktree/*` is not shared, and `git stash create` returns a
stash commit without writing `refs/stash` — but it is not needed once workers cannot stash at all.
Consequence to accept: `worktrees.py:130` still raises when a merge refuses because of uncommitted
local changes; that path is addressed by Change 6 instead.

**Second, previously unguarded instance of the same class:** `create_worktree`
(`execution/worktrees.py:72`) does not set `extensions.worktreeConfig`, so **git config is shared** —
a worker running `git config user.email` mutates the operator's repo config.

______________________________________________________________________

## Change 6 — `WorktreeError` is not a terminal failure

`scheduler.py:445` classifies harness-blocked failures as `INTERRUPTED`:

```python
except (SessionError, LlmProcessError, PermissionDenied) as exc:
    return self._classify(gid, GroupState.INTERRUPTED, ...)
except Exception as exc:            # WorktreeError falls through to here
    final = self._classify(gid, GroupState.FAILED, ...)
```

A refresh conflict is a real-world block awaiting a human merge, not wrong work — identical in kind
to `PermissionDenied`, whose own comment reads *"a denial is the harness reporting a real-world
block, not the coder's work being wrong."* g1's two commits were valid throughout; the group went
terminal anyway and needed an escalation to recover.

**Change:** `_refresh_onto_tip`'s conflict branch raises `WorktreeRefreshConflict(WorktreeError)`,
which joins the `INTERRUPTED` tuple. Other `WorktreeError`s stay terminal — *"path exists but is not
a worktree on `<branch>`"* is a genuine environment misconfiguration.

______________________________________________________________________

## Change 7 — round-atomic bookkeeping and config drift

**Bookkeeping.** Nothing persists until a round completes, so `started_at` is always `None` and an
in-flight group is byte-identical to one that never started. The `heartbeat.json` shipped in
`obsprov1` cannot deliver its own stated payoff ("started 54m ago, 0 rounds completed") because there
is no start timestamp to read. **Write `started_at` / `model` / `transcript_path` at session
*creation*.** With Change 1, per-turn usage lands continuously rather than only at round end.

This also repairs a premise the Observatory relies on: after g1 died on a usage limit **and** was
resumed, `manifest.json` still held **one** session entry, not two. An attempt grid built purely on
`manifest.sessions` under-reports exactly the failures the operator most wants to see.

**Config drift.** Two `.orchestrator/config.toml` files exist with different values for the same
knob — `smart_mcps` has `context_token_limit = 200000` (2026-07-29), `smart_mcps-fe-test` has
`120000` (2026-07-22). `.orchestrator/` is gitignored, so the update never propagated. Operator and
assistant were reading different ground truth mid-conversation. **Echo the resolved config path and
key values at run start.**

Note this did not cause the 306k blowout: the breaker is consulted only in `_reenter`, so a 200k
setting would have changed nothing. The mid-round bound (Change 3) is the real fix.

______________________________________________________________________

## Deliberately out of scope

- **Containers per worker.** Landlock plus deny-rules covers the observed failure mode; containers
  re-open the hole anyway if `$HOME` is volume-mounted.
- **Private per-worktree stash.** Superseded by denying stash outright.
- **Automatic escalation timeouts.** Explicitly rejected — a human always decides.
- **Turn ceilings.** No `--max-turns` on this CLI; the token ladder is the chosen lever.

## Known unknowns

- **The real context window.** A single request carried 306,157 tokens, which a 200k window cannot
  accept — so these sessions run on something larger. The ladder does not depend on knowing it, but
  the headroom argument does.
- **The exact `~/.claude` runtime-dir allowlist.** To be measured, not guessed.
- **Whether a forced wrap-up degrades report quality.** Unmeasured in the literature, and cheaply
  measurable in this harness.
