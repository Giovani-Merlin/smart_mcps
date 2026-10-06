---
date: 2026-10-06
topic: worker-friction-and-live-breakers
---

# Worker friction, live breakers and the export the analyses need — Requirements

## Summary

Run r20261006-050234 (infinity-skills, plan `2026-10-05-001`) produced the first
measured picture of what orchestrator workers actually lose time on across ten
runs in four repos: 1,087 failed tool calls in run sessions, of which 155 are a
compound Bash call refused because one subcommand touched a path outside the
worktree, 126 are writes or reads blocked outside the allowed directories
(mostly `/tmp`), 47 are shell-safety heuristics (`$(…)`, newline-`#`,
`cd && git`), 39 are `Edit` string mismatches pasted from a diff, 38 are
non-allowlisted programs retried verbatim, 25 are truncated `Read` tool-call
JSON, and 26 are cancelled siblings of one failing parallel call. On the flow
side: 19 stalls (one session carries five), 23 loops of which 13 are `ls`/`find`
probes, 7 of 17 respawns are context-limit retirements of which two overshot
the 250,000-token limit by 53% and 167%, 24% of reads are redundant, and
verification omission is 7.7%. Every token cost in those documents is a proxy
because the export carries no per-turn usage.

This work removes the friction that is a config or prompt change, adds the
three breakers the data ranks as worth building, and extends the export so the
next analysis pass measures instead of estimating. It changes no analysis in
infinity-skills (that is the companion document there) and injects no memory.

Sources: `infinity-skills/docs/research/2026-10-05-repeated-errors-fixes.md`
(F1–F7), `…/2026-10-05-session-flow-findings.md` (F1–F7, detector table),
`…/2026-10-05-memory-feedback-readiness.md` (Ingestion gaps),
`infinity-skills/.orchestrator/notes-r20261006-050234.md`, and
`infinity-skills/REPORT.md` §25.

## Problem Frame

The worker allowlist is right about *programs* and wrong about *paths*:
`Bash(ls *)`, `Bash(cat *)`, `Bash(find *)` are granted, yet `ls /tmp/x` or
`cat ../other-run/report.json` inside a compound command refuses the whole
command, and a headless worker cannot answer the approval prompt. The
leaderboard therefore shows `search:other:ls` as the costliest template in
eight of ten runs while the cause is the path, not the program. The same holds
for `/tmp`: the ground rule "never write to `/tmp`" exists since 2026-09 but
the sandbox still defaults `TMPDIR` to `/tmp`, so `mktemp` and tool temp files
land outside the write set regardless of the prompt.

The breakers are a different problem. The orchestrator already retires on
context tokens, but it reads the count at round boundaries, so a coder that
grows past the limit inside one long round is retired at 666,490 tokens
instead of 250,000. Nothing watches for a session repeating the same failing
command (five identical `timeout` stalls, 98 observations, no change in files
or signature), and nothing checks that a coder's last edit was followed by a
verification command before its report is accepted.

Finally, the analyses that found all this ran on proxies: cost per error is
occurrences × observations-to-next-progress × a session-level tokens-per-turn,
because the export's `events/*.jsonl.gz` carry no `usage` per assistant turn,
no heartbeat or peak-context fact, and no link from a denial or a surprise to
the observation it was about.

## Key Decisions

- **Paths, not programs, are the allowlist change.** The worker gets the
  run's own directory (`.orchestrator/runs/<run>/`) and every path a
  `Run (driver, sandbox-safe):` item names as `permissions.additionalDirectories`
  (read scope). `/tmp` is never added; `TMPDIR` moves inside the worktree
  instead. Rejected: granting `/tmp` (widens the sandbox, loses evidence on
  restart — CLAUDE.md), and a bare-program allowlist widening (already granted,
  not the cause).
- **Prompt rules are the fix where the error text is a Claude Code heuristic.**
  `cd X && git`, `$(…)` and newline-`#` inside quoted args, and `Edit` from a
  diff are all refusals or mismatches the worker can avoid by form. Rejected:
  trying to pre-validate Bash text in the orchestrator (duplicates a moving
  CLI heuristic).
- **Three breakers, in the order the data ranks them; two detectors not built.**
  Per-turn token-limit evaluation (fires already, timing is the defect), a
  stall breaker keyed on the exact command string plus error signature
  (warn-then-retire), and a verification-omission gate at report time. The
  redundant-read reminder is advisory only. Ping-pong, unresolved-retry and
  wasted-respawn breakers are **not** built: zero positives in 98 sessions and
  17 respawns.
- **The loop key is `(full command string, error signature)` and ignores the
  `search` action class.** With the current `normalized_command` key 13 of 23
  loops are `ls`/`find` path probes — an estimated 57% false-positive rate,
  unusable as a hard breaker.
- **The export grows additively.** Per-event `usage`, `heartbeat`/peak-context
  facts, a `denial_obs_id` on coder-report meta, structured surprise and
  escalation rows, and a `wait` tag on background-process polling. The
  bundle schema version increments; every existing consumer keeps working
  with the fields absent.
- **A repeated identical denial ends the attempt.** The third identical denied
  command in one session closes the round as `permission_denied` with the
  denial kind, instead of burning turns — the cost of F6 is the retries, not
  the first denial.

## Requirements

### Sandbox and allowlist friction

- R1. `read-roots` — **Workers get the paths they actually read as trusted
  directories.** The session runner emits `permissions.additionalDirectories`
  (or `--add-dir`) for `.orchestrator/runs/<run_id>/` of the target repo and
  for every path named by a `Run (driver, sandbox-safe):` item of the group,
  resolved at launch. Evidence: 155 "contains multiple operations … requires
  approval" refusals in 9 runs / 61 sessions, all on out-of-tree paths inside
  granted programs (F1). Acceptance: a compound `ls <run dir> && grep …` from a
  confined worker exits 0 in a live probe; `/tmp` is not in the list.
- R2. `tmpdir-inside-worktree` — **`TMPDIR` and `TMP` point at
  `<worktree>/.coder-scratch/` in every worker spawn.** `confinement.py`
  already includes `$TMPDIR` in the write set, so Landlock and the sandbox
  agree. Evidence: 126 "allowed working directories" blocks and 10 "Output
  redirection to '/tmp/…' was blocked" (F2). Acceptance: `mktemp` inside a
  confined worker returns a path under `.coder-scratch/`.
- R3. `shell-form-rules` — **The worker ground rules forbid the three shell
  forms Claude Code's static heuristics refuse.** `git -C <abs>` instead of
  `cd X && git`; no `$(…)`, brace expansion or newline-`#` inside quoted
  arguments; multi-line Python goes in a file under `.coder-scratch/`.
  Evidence: 37 `simple_expansion`, 12 untrusted-hooks `cd && git`, 10
  newline-`#` refusals across 9 runs (F3). Acceptance: the rules are in
  `orchestrator/prompts/worker_ground_rules.md` and the prompt snapshot test
  carries them.
- R4. `edit-from-read` — **The ground rules require a `Read` of the target
  span immediately before every `Edit`, and `old_string` copied from that
  output, never from a diff.** Evidence: 39 "String to replace not found" in 8
  runs; sampled inputs begin with a `- ` diff marker (F4). Acceptance: rule
  present; the live tier shows no string-mismatch error in a probe session
  that edits a file it has just read.
- R5. `repeat-denial-cap` — **The third identical denied command in a session
  ends the round as `permission_denied` with the classified kind.**
  `denial.py` already classifies; it gains a per-session repeat count keyed on
  the normalised denied command. Evidence: `claude -p "Say the word OK …"`
  issued three times verbatim; `bash:other:claude` has the highest
  unrecovered rate (25%) in the top 25 (F6). Acceptance: a stub session that
  repeats one denied command three times ends with `permission_denied` and a
  log line naming the command and count.
- R6. `short-worktree-slugs` — **Group worktree directory names are capped so
  path arguments stay short.** Evidence: 25 `Read … could not be parsed as JSON` with the payload cut mid-path at ~169 bytes inside 100+-character
  worktree paths (F5, low confidence on cause). Acceptance: the slug after
  `<gid>-` is at most 24 characters; existing runs' paths are unaffected
  (resume reads the recorded path).

### Live breakers

- R7. `per-turn-token-check` — **The context-token limit is evaluated on
  every `on_turn`, not at round boundaries, and a handoff is staged at 80%.**
  Evidence: 7 of 17 respawns are context retirements; two overshot the
  250,000 limit by 53% (382,203) and 167% (666,490) (flow F4, detector rank
  1). Acceptance: a stub session whose turns grow past the limit mid-round is
  retired within one turn of crossing it; the handoff prompt exists before
  retirement when 80% was crossed earlier.
- R8. `stall-breaker` — **A session repeating the same `(command string, error signature)` with no file, signature or verification change for K=8
  observations gets one "this command is failing identically; change
  approach" injection, and is retired after a second window.** Pure
  read/search windows need 2K=16; the `search` action class is excluded from
  the loop key. Evidence: 19 stalls in 14 of 98 sessions, 5 in one session on
  `config-deps:exit_code:timeout` / command `true` (flow F1, rank 2); 57% of
  current loops are `ls`/`find` probes (F2). Acceptance: the stub session from
  the stall fixture is warned once and retired on the second window; a
  session of 12 distinct `ls` probes is not.
- R9. `verification-omission-gate` — **A coder report whose last edit has no
  later verify-class action is not accepted until the group's `check_command`
  has run.** Cheap: reuses the preflight command. Evidence: omission rate
  0.0769 among coder sessions with an edit (flow F6, rank 3). Acceptance: a
  report after `edit, edit, report` triggers the check command and the round
  log says so; `edit, test, report` does not.
- R10. `redundant-read-advisory` — **A second `Read` of the same file with no
  intervening edit returns a one-line reminder naming the earlier turn; never
  blocking.** Evidence: 345 of 1,431 reads redundant (24%), smart_mcps 30%
  (flow F5, rank 4). Acceptance: the reminder appears once per file per
  session in a stub transcript; a re-read after an edit does not trigger it.

### Export and record

- R11. `per-event-usage` — **Every assistant event in `events/*.jsonl.gz`
  carries `usage: {input, output, cache_read, cache_creation}`.** Evidence: all
  cost figures in the three findings documents are proxies (~129k–174k tokens
  per turn, session-level); the cache-read ratio is saturated at 1.0 in every
  scope so the context-waste proxy cannot rank sessions (every Ingestion gaps
  section, item 1). Acceptance: the fixture bundle's events carry the field;
  `ingest.json.schema_version` increments; older bundles still ingest.
- R12. `peak-context-and-heartbeat` — **The bundle carries each session's
  `last_context_tokens`, peak context, and the heartbeat phase timeline.**
  Evidence: the two overshoots cannot be localised to a turn; liveness is not
  measurable from the bundle (memory-readiness gap 8). Acceptance: present in
  the fixture bundle under the session entry.
- R13. `denial-and-surprise-links` — **A coder-report meta row carries
  `denial_obs_id` for the observation it denies, and surprises and
  escalations export as structured rows with `session_id` and `seq`.**
  Evidence: `framework:coder_report` rows hold `denied_command` but no link
  (error-fixes gap 4; flow gap 2). Acceptance: the fixture bundle shows one
  denial linked to its observation and one surprise row with a seq.
- R14. `wait-action-tag` — **Polling a background process (`kill -0`,
  `sleep`-loops, `tail -f`, `nohup` checks) is tagged `wait` in the exported
  event so analyses exclude it from loop counts.** Evidence: `claude`, `kill`,
  `nohup` loops near retirements are process polling, not failures (flow F3).
  Acceptance: the fixture bundle tags one polling command `wait`.
- R15. `rewrite-counter-persisted` — **`state.json` carries each group's
  `rewrites` count and whether the last rewrite was counted.** Evidence: item
  g6-3 of r20261006-050234 could not verify "not counted against
  `max_rewrites`" from any persisted fact. Acceptance: visible in `status`
  and in the exported bundle.

## Non-Goals

- **No memory injection, no A/B.** The readiness document says the Run Set is
  not ready (0 skill candidates); the companion infinity-skills work aligns
  signatures first.
- **No change to infinity-skills analyses or enrichment** here.
- **No `/tmp` in any allowlist**, and no bare-program allowlist widening.
- **No ping-pong, unresolved-retry or wasted-respawn breakers** (zero
  positives).
- **No pre-validation of Bash text** in the orchestrator.

## Open Questions

- Q1. Should R1's `additionalDirectories` cover sibling runs' directories
  (`.orchestrator/runs/*`) or only the current run's? The samples read other
  runs' reports; the wider grant is read-only but broad.
- Q2. R9 as a hard gate or a warning for the first run? The base rate is
  low (7.7%) and the check command costs ~3 minutes on infinity-skills.
- Q3. R8's thresholds (K=8, 2 windows) are "as measured" on 98 sessions with
  no hand labels; accept them for a first live run and tune in the eval
  harness, per the smart-CLI preference?

## Next Step

`/orchestrator-plan docs/brainstorms/2026-10-06-worker-friction-and-live-breakers-requirements.md`
from a smart_mcps session, after PR #16 (the five small fixes, plugin 0.21.1)
is merged and the tool reinstalled.
