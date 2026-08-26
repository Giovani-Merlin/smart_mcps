# Observatory front-end findings — 2026-08-20 session

Context: driving the Observatory (`smart-mcps-orchestrate ui`) while grouping
drummAI's `docs/plans/2026-08-20-001-feat-app-shell-and-pages-plan.md`, ahead of
launching that run from the UI. Registry: `~/.orchestrator-ui.yaml` with
`drummAI` and `smart-mcps`.

## Front-end

### F1 — `--registry` default was never wired (FIXED, uncommitted)

`_cmd_ui` passed `registry_path=args.registry`, which is `None` when the flag is
omitted, and `load_registry` only reads a file when the path is not `None`. So the
documented default `~/.orchestrator-ui.yaml` was unreachable and **every** launch
silently fell back to the single `--repo` project. `default_registry_path()` had
zero callers. Fixed in `orchestrator/cli.py`; `/api/projects` now returns both
projects, 307 ui/registry/observatory tests pass.

### F2 — a group job shows nothing at all for minutes, with no progress

`JobLog` streams `/events/job` correctly (verified by replaying job
`j20260820-211242-28b97c` with curl), but the **`group` CLI writes nothing until
it finishes**: the job log file was created at 23:12:42 and received its first
byte at 23:16:17 — 3m35s of "Waiting for output…" that is indistinguishable from
a hang. Two halves to fix:

- orchestrator: emit stage progress (mapper → graph → partition → specs *i*/*N*),
  unbuffered, so there is something to stream;
- UI: a determinate progress bar driven by those lines, plus an elapsed timer.
  `spec i/N` is the only long stage and it is countable, so a real percentage is
  available rather than a spinner.

### F3 — the launch page never refetches; a finished grouping stays invisible

`Launch.tsx` fetches plans/groupings/runs once in a `useEffect` keyed on
`[project]`. A grouping that completes while the page is open does not appear,
and **"Start a run" cannot see the grouping that was just created** — which reads
as "my grouping was lost". Fix: refetch the three lists when a job transitions to
not-running (the job stream's end is the natural trigger).

### F4 — the group form cannot express `granularity` or `token_budget`

`POST /jobs/group` accepts `granularity`, `token_budget` and `auto_resume`;
`GroupCard` only offers plan, name and dry-run. Trying `--granularity balanced`
from the UI is impossible, so that experiment sends you back to the terminal —
the exact thing the launch page exists to avoid.

### F5 — a launched job is unaddressable and dies with the page

The job lives in `Launch` component state. There is no `/p/:project/jobs` route
and no `/p/:project/jobs/:id`, so a refresh, a navigation, or a second browser tab
loses sight of a job that is still running. `GET /api/projects/{p}/jobs` already
returns everything such a view needs.

### F6 — the job's `running` flag is frozen at POST time

`JobLog` renders `job.running` from the POST response and never refetches it, so a
finished job keeps reporting "running" until the page is reloaded.

## Orchestrator

### F7 — grouping is not reproducible: the codegraph index drifts under a fixed commit

From `drummAI/.orchestrator/grouping-metrics.jsonl`, all at commit `4d6e6f53`
with a clean tree and the same plan:

| time (UTC)       | groups | critical path | index fingerprint |
| ---------------- | ------ | ------------- | ----------------- |
| 20:53–20:54 (x3) | 16     | 8             | `d4ca6494f70a`    |
| 21:09            | 16     | 8             | `d4ca6494f70a`    |
| 21:13            | 13     | 8             | `dcb773359c48`    |
| 21:14–21:24 (x8) | 13     | 8             | `5e2fd985f6ad`    |

Three fingerprints in fifteen minutes at one commit — and `codegraph sync` in that
repo reported "Already up to date" at 21:05, minutes before the first shift. The
partition follows the index, so the group count a operator approves is not the one
that runs.

Consequence for this session: the 16 → 13 drop was **not** `--granularity
balanced`. Re-running `--no-spec --granularity independent` at the current index
also yields exactly 13, with the identical membership. Granularity was a no-op on
this plan; the index changed.

Worth deciding: pin the partition to a recorded `index_fingerprint` and refuse (or
loudly flag) a run whose index no longer matches the grouping's, the way
`worktree_dirty` and `repo_commit_sha` are already recorded but not enforced.

### F8 — `--no-spec` / `--dry-run` corrupt a named grouping directory

Both modes deliberately write `grouping-trace.json` and `edge-provenance.json`
into `grouping_dir(repo, name)` ("--no-spec is the debugging mode, so it is exactly
when the reasoning is wanted most") but write no `groups.json`. Two consequences,
both observed:

- Run `--no-spec` after a real grouping and the directory holds a **16-group
  `groups.json` beside a trace describing a 13-group partition** (mtimes 23:12:47
  vs 23:24:08). The Observatory's grouping tab and the edge-provenance view then
  render a partition that is not the one in `groups.json`.
- Run `--no-spec` before any grouping and the directory looks like a failed
  grouping: trace + provenance, no `groups.json`, and `describe_groupings` skips
  it, so nothing lists it. This was the state at session start.

Fix: give the specless modes their own directory (`groupings/<name>/preview/` or a
`--trace-dir`), or refuse to write into a directory holding a `groups.json` from a
different partition.

### F9 — `--name` is correct

`group --name 2026-08-20-001-app-shell-balanced` created a second directory and
overwrote nothing; both groupings are listed by `/api/projects/drummAI/groupings`.

### F10 — packaging

`uvicorn` is imported by `_cmd_ui` but is not declared in `pyproject.toml` (it
arrives transitively via fastmcp). The installed uv tool env
(`~/.local/share/uv/tools/smart-mcps`, editable, from before fastapi was added)
has no fastapi at all, so `smart-mcps-orchestrate ui` fails there with
"the Observatory needs fastapi and uvicorn installed" until a
`uv tool install --force`.

## Second sitting — the first live run from the UI (`r20260820-213134`)

Launched from the launch page against the balanced grouping, every execution
option left unspecified. It halted before any group produced work. Sequence, from
`runs/r20260820-213134/logs/run.log`:

1. 23:31 run starts, g1 (`design-system`) forks the base session.
2. 23:43 **session limit** — `usage-limit.json` armed, "resets 12:40am".
3. The gate waits **57m52s**, logging every ~75s, then wakes and retries the call.
4. 00:41 the retry dies with `401 OAuth access token has expired. Re-authenticate
   to continue.` → g1 `interrupted` → `on-failure halt` holds all twelve other
   groups (`run_halted`) → run over, 0 groups completed.

### F11 — the run banner names a config file that does not exist

`config: /home/gbm1996/wksp/drummAI/.orchestrator/config.toml (token_budget=200000, …)`
— there is no such file. The values printed are the library defaults. Printing a
path implies a file was read; it should say "defaults (no config file)".

### F12 — the snapshot drops the two heartbeat fields that explain a long phase

`GroupHeartbeat` is documented as "the group's `heartbeat.json`, passed through
unchanged", but the model omits `paused_s` and `round_elapsed_s`, both of which
are on disk. g1's file records `phase_elapsed_s: 3529` **with `paused_s: 3472.4`**;
the API serves only the first. The board therefore shows "forking the base
session — 58m" with nothing to say that 57 of those minutes were a deliberate
usage-limit pause. (The run-level `UsageLimitBanner` does cover the operator here,
and the stall inference does *not* misfire, because the heartbeat's `updated_at`
keeps advancing through the pause. This is a legibility gap on the group card, not
a wrong verdict.)

### F13 — a re-auth error after a usage-limit wait kills the whole run

The gate slept an hour and then made exactly one attempt, with an OAuth token that
had expired *during the sleep*. Two things follow:

- Nothing re-validates auth after a long pause, which is precisely when a token is
  most likely to have gone stale.
- "Re-authenticate to continue" is operator-recoverable, not fatal, yet it is
  classified as an envelope `SessionError` and takes 13 groups down with it. The
  usage-limit gate already knows how to pause and wait for a human-fixable
  condition; a 401 wanting re-auth belongs in that class (pause + notify, or
  escalate when HITL is on), not in the halt class.

Verified 2026-08-21 10:19 that the credential is healthy again (`claude -p`
answers), so the run is resumable as-is — nothing else about it is broken.

### F14 — a run launched from the UI is serial by default

The job record shows every execution option as `null`/`false`, so `concurrency`
fell back to the library default of **1**: thirteen groups, one at a time, on a
DAG whose widest wave is three. Nothing on the form suggests that is what "leave
it unspecified" means. Worth surfacing the resolved values in the form (or in the
run header) rather than leaving the operator to read the run banner afterwards.

## Third sitting — the resumed run, and why g1 could never merge (2026-08-21)

`r20260820-213134` resumed at 08:25Z and was dead by 08:33Z with
`GroupFailure: rewrite cap (2) exhausted`. g1's work was never the problem: the
branch `orchestrator/r20260820-213134-g1` carries two real commits (Tailwind v4 +
dissolving `styles.css`, then the vendored shadcn primitives). Every generation
reached the merge attempt and every merge attempt died in preflight:

```
2 failed, 537 passed, 9 skipped
FAILED tests/test_separate.py::test_bs_roformer_output_dir_reaches_the_loaded_model
FAILED tests/test_separate.py::test_drumsep_output_dir_reaches_the_loaded_model
  SeparatorUnavailable: audio-separator not installed
```

### F15 — root cause: the worktree venv cannot see a hand-installed package

`audio_separator` 0.44.5 was installed by hand into `drummAI/.venv` and declared
in no dependency table. Worktrees are provisioned with `uv sync --all-extras`
(`ExecutionConfig.provision_args`) **from the lock**, so they came up without it,
and a suite that passes for the operator fails for every group. Fixed in drummAI
(`ffa19a8`): declared as the `separate` extra carrying the `[gpu]` marker its own
error message recommends. Two knock-ons worth recording — `audio-separator`
requires `numpy>=2` while `muscriptor` pins `numpy<2` on darwin/x86_64, so the
lock is now Linux-only via `[tool.uv] environments`; and the resulting numpy 2.4.6
/ torch 2.13.0 are what the root `.venv` already had, i.e. the lock had drifted
behind the environment the tests actually pass in. Full suite: 548 passed.

BS-Roformer, one of the two tests involved, is still exploratory and not in use:
it does not separate drums from bass, so its drum stem is not yet usable for
transcription.

### F16 — a preflight failure is never classified, so an unfixable one costs everything

Every failure arriving at the merge gate is treated identically: escalate (a no-op
with HITL off), then **rewrite the spec and fork a fresh coder generation that
redoes work already committed**. A missing dependency in the venv is not a defect
in the group's diff and no coder can repair it, yet it consumed generations 2, 3
and 4 plus both spec rewrites before failing the run.

Proposed: put a small, cheap LLM classification call on the preflight/merge
failure path, with three outcomes:

| verdict                                                                 | action                                                                                        |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| environment / pre-existing (missing dep, unrelated red test, infra)     | do **not** rewrite; fail the group fast with the diagnosis, or pause the run for the operator |
| the group's own defect (its diff broke a test, a conflict in its files) | today's behaviour — rewrite and respawn                                                       |
| ambiguous / unrecognised                                                | interrupt and ask the operator (an escalation with the check output attached)                 |

A cheaper non-LLM half of the same idea, worth doing regardless: **record a
baseline** of the check command's result on the launch branch at run start, and
only count failures that are *new* relative to it. Both of g1's failures were red
before the run began.

### F17 — the naming `…-g1-coder-g3` reads as "group 3"

Session ids are `<run>-<group>-<role>-g<generation>`, so `g` prefixes both the
group and the generation and `r20260820-213134-g1-coder-g3` looks like it involves
group g3. Render the generation as `gen 3` in the UI (and consider `gen3` in the
id itself).

## Fourth sitting — the resume after the weekly limit (2026-08-25)

The run came back up on 2026-08-25 at 11:28 and re-entered g9 by resuming its
interrupted coder session, exactly as intended: g1, g3, g4, g5 and g8 are merged,
g9 is running, the rest are held on DAG dependencies. Execution is healthy. Two
things the Observatory shows about it are wrong, and both come from the same
root — **a resume never rewrites the run's on-disk record, so the UI keeps
describing the run as its original launch left it.**

### F20 — a resumed run's escalation config is stale in the snapshot

`run.log` for this resume says, in its own words:

    run r20260820-213134 started with HITL: intensity=on_stuck, source=workers_via_orchestrator

The snapshot the UI reads says the opposite:

    "escalation": {"enabled": false, "intensity": "autonomous", ...}

Both are honest about their own source. `_load_config` puts CLI flags *above* the
persisted tier, so the resumed process really is running with HITL on — but
`RunManifest(...)` / `store.save(manifest)` sit inside the branch that establishes
a base session (`cli.py:1182`), and a resume reuses the existing base session and
skips that block entirely. The manifest therefore still carries the *first*
launch's `escalation` and `usage_limit`, and `/snapshot` serves the manifest.

The operator-visible consequence: the Observatory says HITL is off while workers
are free to escalate, so nobody is watching for a prompt that can now arrive. The
fix is to persist the effective config on resume too — the manifest write wants to
move out of the base-session branch, or gain a resume-side update of the fields
that CLI flags can override. Note this also undercuts R6's assumption that the
manifest is a trustworthy source for pre-filling a resume form: today it records
the launch config, not the last-used config.

### F21 — a usage-limit pause survives the process that armed it, so a live run renders as paused

`usage-limit.json` still reads:

    armed_at 2026-08-21T20:54:07+02:00 · wake_at 2026-08-21T21:09:07+02:00 ·
    released_at null · "You've hit your weekly limit · resets Aug 25, 1pm"

`released_at` is only written by the gate that armed it, and that process died
while still paused, so the record is stuck in its armed shape forever. Nothing on
resume clears or re-arms it. `UsageLimitBanner` computes
`paused = Boolean(usageLimit && !usageLimit.released_at)` and renders on that
alone, so while g9 is actively working the run page is showing **"Paused — usage
limit · resumes 21/08 21:09 · any moment now"**. The banner's own comment says it
"clears itself without needing a second signal" — true within one process, false
across a resume.

This is the mirror image of the known "a run dead-while-paused looks alive"
failure: here a live run looks dead. Cheapest correct fix is for resume to stamp
`released_at` (or drop the file) when it starts work, since by definition the
pause the record describes is over; a UI-side guard on a `wake_at` far in the past
would only paper over it, and would mis-handle a genuinely long weekly reset.

### F22 — the run log stamps UTC but quotes reset times in local, in the same line

    2026-08-25T16:13:38+00:00  group g12: still paused: usage limit until 2026-08-25T18:20+02:00

Both halves are correct and they are two hours apart. The line prefix is UTC; the
reset instant is passed through in the provider's own zone (Europe/Berlin, +02:00),
which is right for F-series reasons — the provider's wording is evidence and must
not be paraphrased — but putting the two side by side reads as a four-hour
bookkeeping error at a glance, and it cost real time chasing one. The elapsed and
paused counters on the same line are correct.

The log is the operator's surface, not a machine's: stamp it in the operator's
local zone (or render both prefix and reset in the same zone and label it once at
the top of the file). The Observatory should do the same wherever it prints a raw
timestamp.

### F23 — estimate-vs-outcome silently reports the generation that survived

The calibration table is meant for exactly one purpose — reading, by hand, whether
the estimator is any good — and on multi-generation groups it reports the opposite
of what happened. `groupPrediction` takes *the latest coder session with a recorded
occupancy* as the outcome. That choice is well-reasoned for the quantity's sake
(the estimate describes one coder's context, so summing generations would rebuild
the cross-quantity sum the module correctly refuses to compute), but the latest
generation is, by construction, the one that *fit*. This run:

| group                         | estimated | last-gen           | peak                | what happened                                |
| ----------------------------- | --------- | ------------------ | ------------------- | -------------------------------------------- |
| g9                            | 150 359   | 98 811 (**0.66x**) | 257 017 (**1.71x**) | gen 1 retired: context 257017 > limit 250000 |
| g1                            | 152 903   | 80 603 (**0.53x**) | 128 717 (**0.84x**) | four generations                             |
| every single-generation group | —         | 0.52x–1.24x        | same                | honest                                       |

So the two groups whose estimates were *worst* are the two the panel flatters
most, and g9 — the one group that actually broke the context limit — is presented
as a 34% over-estimate. The single-generation rows are trustworthy; the summary
median and aggregate are computed over a mix of the two kinds and are therefore
neither.

Not to be fixed by summing. Options to decide between:

1. **Segregate.** Multi-generation groups leave the calibration table and appear in
   `skipped` with a stated reason ("3 retired generations"), the same way rows with
   no estimate already are. Keeps the summary figure honest and unstated-subset-free.
2. **Report the peak.** Use the maximum occupancy across generations — still one
   coder's context, so the quantity stays sound, and it is the number the estimator
   should have predicted if the group was to fit in one pass.
3. **Label, don't hide.** Keep the row but carry `generations` and
   `retirement_reason` into it, so a manual read sees "0.66x (gen 2 of 2; gen 1
   retired at 257k)" and can judge for itself.

(3) is the minimum, since the stated purpose is manual analysis. (1)+(3) together
are probably right: label every row, and exclude the labelled ones from the
median/aggregate rather than from the table. Worth pairing with the F19 cost-model
work — same panel, same "which number means what" problem.

### F24 — the `-extra` verdict is correct behaviour with no surface that explains it

`verdict-g1-r1-extra.json` on g7 is the mandatory second verification pass
(origin R15): a group whose difficulty exceeds `d_hard` (0.65) gets
`paired_plus` intensity, and on the reviewer's *approval* one extra pass runs
before the approval is accepted. g7 scored **0.7349** and is the only group in
this grouping above the line — every other group is `paired` or `self_verify`,
which is why this file appears exactly once in thirteen groups.

Nothing surfaces that. The run log says `(extra pass)` in one line; the artifact
name says `-extra`; the difficulty score and the intensity tier that caused it are
in `groups.json` and are shown nowhere in the drill-in. An operator meeting this
file has no way to learn why this group and no other got a second opinion. The
group drill-in should show `intensity` and `difficulty` next to the group, and
label the extra verdict as what it is.

One consequence worth knowing, benign today: `finish.py`'s `_VERDICT_RE`
(`^verdict-g(\d+)-r(\d+)\.json$`) deliberately does not match `-extra.json`, so
the PR body reports the *first-pass* verdict for a `paired_plus` group. Harmless
as long as an extra pass that flips to `changes_required` sends the group back to
rewriting (it does, so a completed group's two verdicts agree), but the
`(generation, round)` key is genuinely ambiguous between the pair.

### F25 — the surprise board silently strands cross-group notes, and nothing reports it

At the end of this run `surprises.json` holds **24 undelivered surprises across
seven buckets**, and four of those buckets are not groups:

| bucket                                     | entries | what it is                                                                           |
| ------------------------------------------ | ------- | ------------------------------------------------------------------------------------ |
| `u16-play-route`, `u10-calibration-passes` | 17      | **task/unit ids, not group ids** — workers named the affected work in plan U-numbers |
| `g14`, `g15`, `g16`, `g17`                 | 6       | **group ids that do not exist** — this grouping has 13 groups                        |
| `g10`                                      | 3       | a real group that had **already completed** when the notes were written              |

`SurpriseBoard.mark` does `self._pending.setdefault(gid, []).append(surprise)` for
every id in `affected_groups`, with no check that the id names a group in this
run. The class docstring is honest that "marks for completed/failed groups are
simply never read" — that is a deliberate design choice and fine — but it silently
extends to ids that could never be read by anyone, and the run finishes without a
word about it.

The g10 case is the one with teeth. Both of g7's verdicts carry the same note: g7
changed `web/src/upload/Upload.test.tsx`, a file g10 owns, because g10's readiness
wait counted three comboboxes and let the form submit with `backend=''` before the
fetches resolved. The change is test-only and strictly stronger, so nothing broke
here — but the owning group was told nothing, because g10 merged at 16:44 the
previous day and g7 wrote the note the next morning. A late-discovered defect in an
early group's tests has no route home.

Three separate fixes, in priority order:

1. **Validate on mark.** An `affected_groups` id that names no group in the run is
   a worker mistake; log it at mark time and route it to the run's surprise list
   rather than a dead bucket. Cheap, and it would have caught 23 of the 24.
2. **Report the residue.** `finish` (and the run's end-of-run summary) should list
   what is still pending on the board, by bucket, with "never delivered — group
   already completed" or "unknown group id" stated. Right now this file has to be
   read by hand to know any of it exists.
3. **Dedupe.** `u10-calibration-passes` holds the same `PassRun` note five times
   and the same `useMidi()` note five times, re-emitted by each round and
   generation in slightly different wording. Even delivered, that is noise in a
   coder's briefing.

The Observatory should surface the board too — there is no surprise view at all
today; `GroupDrillIn` renders a group's own emitted surprises, not what is pending
*for* it.

### F26 — surprises drive spec rewrites, and every part of that is invisible

Answering "what are surprises actually for": they are not notes for a human, they
are the mechanism by which a group's spec is **rewritten mid-run**. `_rewrite`
calls `deps.rewrite_spec(self.group, surprises)`, which folds them in as
`rewrite_context` and re-runs the **speccer LLM** over that one group, replacing
its `name`, `summary`, `spec` and `verification` (`cli.py:1497`). There are three
delivery points:

1. **Before launch** (`review.py:219`) — anything pending for the group rewrites
   its spec before the coder is ever started.
2. **Before accepting an approval** (`review.py:374`) — a surprise that lands while
   the group is in review **vetoes the pending approval**: the group does not
   merge, it re-specs and runs another generation.
3. **On every failure path** — merge conflict, preflight failure, coder
   `needs_input`, `too_hard` — the board is consumed into the re-spec.

So they do exactly what one would hope. The problem is that none of it is
observable, and one property of it is dangerous:

- **Nothing is logged.** `_rewrite` sets `GroupState.REWRITING` and never calls
  `self._log`. `grep -c surprise logs/run.log` over this entire run returns **0**,
  despite the board having been consumed by many groups. A spec rewrite — the
  single most consequential thing that can happen to a group short of failing — is
  absent from the operator's log.
- **The rewritten spec is never persisted.** Group dirs hold reports, verdicts and
  the preflight log; there is no `spec-*.json`. After a rewrite, the spec the coder
  actually worked from exists nowhere on disk — `groups.json` still holds the
  original. Post-mortem cannot reconstruct what the group was told.
- **The rewrite speccer calls are not recorded.** `llm/calls.json` in the run dir
  holds exactly **one** call: the original 13-group speccer pass from grouping time
  (2026-08-20T21:19), carried over by the snapshot. Every mid-run rewrite call is
  unrecorded, so both the token cost and the prompt/response are lost.
- **A broadcast surprise silently spends every named group's rewrite budget.**
  `_rewrite` does `self.rewrites += 1`, and `execution.max_rewrites` is 2. The
  `test_separate.py` baseline surprise in this run named `g2`–`g17` — so every
  group that launched afterwards burned one of its two rewrites, plus a speccer
  call, to be told a test baseline had changed. A group that later hits a genuine
  merge conflict and a genuine preflight failure is then out of budget and fails.
  A cheap "informational" surprise kind that briefs without consuming a rewrite
  would fix this; so would capping fan-out.

Note the id lists needed to validate F25 are already sitting in
`llm/calls.json` under `produced`: `task_ids` (`u1-design-system` …
`u16-play-route`) and `group_ids` (`g1`…`g13`). The stranded buckets
`u16-play-route` and `u10-calibration-passes` are verbatim entries from the
`task_ids` list — workers addressed surprises with task ids from the plan they
were shown, and nothing checked the id against either set.

### F27 — the integration worktree is never provisioned, and nothing says so

Running the finished app from the integration worktree failed, and the diagnosis
offered at the time — "the orchestrator provisioned
`.worktrees/.../integration/.venv` with `uv sync --all-extras`" — is wrong on two
counts, both checkable:

- **The orchestrator never provisions the integration worktree.** `provision_env`
  has exactly one call site, inside `workspace_for` (`cli.py:1359`), which runs
  only for **group** worktrees. `IntegrationMerger.ensure()` calls
  `create_worktree` and stops there. The integration checkout gets a branch and a
  directory, never an environment.
- **That venv was built this morning at 10:00**, after g7 completed at 09:31 local
  and the run ended. It was made by the session trying to run the app, not by the
  orchestrator. Its contents confirm it: `fastapi` and `uvicorn` present, `torch`,
  `muscriptor`, `mt3-infer`, `beat_this` and `audio_separator` all absent — that is
  `--extra app`, not `--all-extras`, which would have pulled ~3 GB of torch.

So the two environments differ by design and nobody is told. **Group** worktrees
get `uv sync --all-extras` (`SessionConfig.provision_args`), which is why workers
could report `pytest 583 passed / 9 skipped` with `test_separate.py` green — their
venvs really did have every declared extra. The **integration** worktree, where an
operator naturally goes to run what was just built, has whatever happens to be
there. The asymmetry is invisible: no log line, nothing in the Observatory, and
group worktrees are deleted on merge so the working example is gone by the time
anyone looks.

Two fixes, and they are independent:

1. **Provision the integration worktree too**, with the same `provision_args`, at
   `ensure()`. It is the tree that represents the run's output; it should be
   runnable. Cheap, and it removes the trap entirely.
2. **Say which environment is which.** Log the provisioning of each worktree with
   the exact `uv sync` invocation used, and surface the worktree path plus its
   provisioning state in the group drill-in. "This worktree was provisioned with
   `uv sync --all-extras` at 21:34" is one line and would have ended the confusion
   immediately.

**A drummAI defect this exposed, same class as `audio-separator` (ffa19a8):**
`demucs` appears **zero times** in `pyproject.toml` and **zero times** in
`uv.lock`, while `htdemucs` is `DEFAULT_SEPARATOR` in `app/pipeline.py:61`. The
app's default separation path depends on a package no manifest declares, so any
`uv sync` uninstalls it from the root venv and no worktree venv ever had it. It
should be declared in an extra exactly as `audio-separator` now is — and the
misleading error is worth fixing alongside: `_demucs_separator` imports `torch`
and `demucs` in one `try`, so a missing `torch` reports "demucs not installed".

## Requested Observatory features

### R1 — the orchestrator's own session belongs on the board and in the history

The board shows a group's coder and reviewer sessions, but the orchestrator's own
work — the base session it establishes, the speccer, the rewrite calls it makes on
a group's behalf — is invisible. It should appear on the group card and in the
attempt-history grid alongside the sessions it drives, so a generation that exists
because the *orchestrator* rewrote the spec is legible as such.

### R2 — the grouping tab should show the speccer's LLM runs

`/p/:project/r/:run/grouping` renders the algorithmic partition — the same thing
`--no-spec` prints — but not the LLM half that turns it into specs. Those calls are
already persisted (the grouping directory's `llm/` records, snapshotted into the
run), so the tab can list them as sessions and open them in the same viewer used
for coder and reviewer sessions.

### F18 — a respawned generation is told the path to the failure, never the failure

`PreflightFailure`'s message is literally
`check command uv run pytest exited 1 — output at <run>/groups/g1/preflight-check.log`,
and that string is all that reaches the next generation: the merge gate wraps it
in `Surprise(kind="other", description=str(exc))`, `_rewrite` folds the surprise
into the rewritten spec, and a fresh coder forks with a **file path** where the
diagnosis should be. The exception even carries `output_path`, and the file holds
the two lines that settle the question outright:

```
FAILED tests/test_separate.py::test_bs_roformer_output_dir_reaches_the_loaded_model
  SeparatorUnavailable: audio-separator not installed
```

Nothing is sandboxing this away — confinement governs writes only, reads are
unrestricted (`confinement.py`: "deny writes outside the worktree — not reads"),
so the coder *may* open the file. It is simply never told to, and never given the
content. Attach the tail of the check output (the `short test summary info` block
is the natural slice) to the surprise, so generation *n+1* opens with the actual
error in context.

This is the half of F16 that costs nothing to build: with those two lines in hand,
a coder would answer "this is a missing dependency in the venv, unrelated to my
diff" on its first turn. The orchestrator would still be the one deciding what to
do about it — which is the F16 discussion — but neither party should be reasoning
from a filename.

### R3 — the finished diff of each generation

When a generation ends, show its diff the way git or VS Code would — the final
state of what that generation changed, not a running feed. It is the only honest
way to see how much a generation actually contributed, and it is exactly what was
missing while g1 burned three of them: generations 3 and 4 re-derived work that
generations 1 and 2 had already committed, and nothing on the board said so.

### R4 — the finished diff of each group

The same view one level up, once a group completes: the whole group's diff against
the integration tip it branched from. The cheapest of the two to build (`git diff
<tip>..<group branch>` is a single call) and the one an operator wants most often.

### F19 — spend is counted from one turn per round, so the cost panel understates it

The cache-write figure is implausibly small, and the reason is a conflation of two
quantities that this codebase otherwise guards carefully.

`RoundUsage.from_envelope` takes **only the round's final turn**:

```python
iterations = usage.get("iterations") or []
latest = iterations[-1] if ... else usage
```

That is deliberate and correct *for occupancy* — the top-level `usage` sums every
turn, which is what produced the 18.6M-against-262k figure that once retired
healthy coders, so the breaker must read the last turn only. But `SessionUsage.add`
then feeds those same last-turn numbers into `total_output_tokens`,
`total_cache_read_tokens` and `total_cache_creation_tokens`, and `cost.ts` renders
those as **spend**. Spend is cumulative over turns; occupancy is a snapshot of one.
A 190-turn round therefore contributes exactly one turn of spend.

The distortion is not uniform, which is why the bar looks wrong rather than merely
small:

- **output** — every turn produces output; only the last turn's is counted. Badly
  understated.
- **cache creation** — written whenever a turn extends the cached prefix, i.e.
  repeatedly through a round; only the last turn's is counted. Badly understated,
  and this is the "impossibly small cache write".
- **cache read** — understated too, but the final turn carries the *largest*
  single read (the full prompt), so it survives comparatively intact. Hence a bar
  that reads as almost all cache-read.

The shape the accounting should have — the operator's model, and it matches the
transcript jsonl:

1. turn 1 opens with a cache read of the inherited context (the forked base
   session's shared prefix) and no write of its own;
2. every subsequent turn has its own cache read, cache write and output;
3. **spend = the sum over all turns** of each class, reported per session;
4. the turn-1 inherited cache read is worth reporting separately, since it is
   context this session did not create and cannot shrink.

Everything needed is already in the envelope's `usage.iterations` — this is a
summation change, not a capture change. Keep `last_context_tokens` exactly as it
is: the breaker depends on it and it is the right quantity for occupancy.

**Before implementing, query Perplexity** to confirm the per-turn billing
semantics — in particular whether `cache_read_input_tokens` on turn *n* includes
the prefix that turn *n−1* paid to write (so summing reads across turns is correct
for cost and not double counting), and how `cache_creation_input_tokens` behaves
across the 5-minute versus 1-hour cache TTLs. The arithmetic above assumes each
turn's read is separately billed; that assumption is the thing to check.

### F13 (root cause found, fixed) — Landlock denied the OAuth token refresh

It recurred: g5 paused **3h58m** on a usage limit, woke at 13:11, retried the call
and died with the same `401 OAuth access token has expired`. Two occurrences, both
immediately after a long pause, is the shape of a token lifetime being crossed —
not of a broken credential.

The cause is in `confinement.py`. `probe_claude_runtime_dirs` allowlists
`~/.claude` entries where `p.is_dir()`, and `~/.claude` itself is never granted
write. `.credentials.json` is a **file at that root**, so a confined worker could
read the token and never write a refreshed one. Proven under the real production
policy before the fix:

```
landlock applied: True abi 3
read  ~/.claude/.credentials.json:  READ_OK
write ~/.claude/.credentials.json:  DENIED
write any other ~/.claude file:     DENIED
```

Fixed by granting that one file read-write — a single file rule, not the
directory, so `settings.json` and `history.jsonl` at the same root stay unwritable
(asserted both ways in
`test_confined_subprocess_can_refresh_its_oauth_credentials`). A worker could
already *read* those credentials long before it could write them, so the exposure
added is a worker corrupting the operator's own token: recoverable by re-login,
against a failure mode that killed two runs.

Caveat: the denial and its removal are proven; that the refresh then *succeeds*
end-to-end can only be shown by crossing a real expiry. The next long pause is the
test. F13's second half stands regardless — a 401 asking for re-authentication is
operator-recoverable and should pause the run, not halt it.

### R5 — choosing the model, for workers, the orchestrator and the speccer

Not selectable in the Observatory, and — worth stating plainly — **not selectable
anywhere**. `SessionConfig.model` defaults to `None`, so `--model` is never added
to a worker's argv (`sessions.py`: `if self.model: argv += ["--model", self.model]`)
and the worker inherits whatever the CLI would pick. No CLI flag exposes it either;
`grep '"--model"' orchestrator/cli.py` finds nothing. The grouper builds its own
argv (`grouping/llm.py`) with `-p/--output-format/--json-schema/--max-thinking-tokens/
--thinking` and no model flag at all.

What actually ran, measured rather than assumed:

| role                | model                                                  | evidence                                                      |
| ------------------- | ------------------------------------------------------ | ------------------------------------------------------------- |
| speccer             | `claude-opus-5`                                        | `groupings/…-balanced/llm/calls.json`, `gen_ai.request.model` |
| coders (g1, g3, g5) | `claude-opus-5` (388 assistant turns; 5 `<synthetic>`) | the three worktree transcripts under `~/.claude/projects/`    |

So it is **Opus everywhere, not Sonnet** — which is worth knowing next to the
session limits this run kept hitting, and next to the standing preference for
cheaper models on analysis-shaped work. Three knobs are wanted, and they are
genuinely different decisions: the coder/reviewer workers (the bulk of spend), the
orchestrator's own base session, and the speccer (one call per grouping, arguably
the one place the strongest model earns its cost). Config fields first, then CLI
flags, then form fields — the Observatory can only offer what the CLI accepts.

### R6 — "Resume a run" should arrive pre-filled

Today the resume card starts blank: pick the run from a dropdown, then re-enter
every execution option. Both halves are already on disk — the job record keeps the
`options` block it was launched with (`.orchestrator/jobs/<id>/command.json`) and
the manifest persists the run's `escalation` settings — so the form can select the
run being viewed and pre-fill the configuration that run last used, leaving the
operator to change only what they mean to change. This is the same class of hazard
that put the shared `ExecutionOptions` block on the form in the first place: a
resume that silently differs from the run it continues.

### R7 — show a grouping's groups on the launch page

"Group a plan" has a dry-run that prints the groups and writes nothing; "Start a
run" has a grouping dropdown that shows only a name and a count. When a grouping
already exists, the same listing should be visible there — group names, tasks,
files, estimates, dependencies — so what is about to be launched can be read before
launching it, without a terminal and without a throwaway dry run.

### R8 — move the worker's invariant rules into the shared context, and leave the fork message customizable

Reading g7's actual prompt makes the split visible. Every forked coder receives,
*after* the 55 KB base context, a message whose only per-group content is the
`<run-manifest>` identity, the `<spec>`, and the verification item list. Everything
else — the ground rules (worktree confinement, `uv sync` inside the worktree,
commit-early-and-often, the permission-denial retry protocol and its
`denial_error` / `denial_source` fields) and the entire report contract — is
byte-identical on every launch: `coder.md` (2 567 B) plus `report_contract.md`
(2 341 B), of which only `$identity_block` and `$verification` vary.

This run forked **17 coder sessions and 12 reviewer sessions**, so roughly
**106 KB of identical instruction text** was re-sent as per-fork content
(`17 x 4.9 KB` for coders, `12 x 1.9 KB` for `reviewer.md`).

Two reasons to move it, and the second is the stronger one:

- **Position.** The invariants currently sit behind the whole plan document and
  codegraph dump. Instructions that govern behaviour work better at the front of
  the context than buried after 55 KB of reference material.
- **Caching.** Workers are *forks of the base session*, so the base context is a
  shared, cached prefix while the fork message is fresh input on every launch.
  Invariant text in the fork message is paid at full price 29 times; the same text
  in the base context is written once and read from cache thereafter. This is the
  same accounting that F19 is about, and the two should be looked at together.

Shape of the change: `base-context.md` gains a leading section carrying the worker
ground rules and the report contract, lightly rewritten to refer *forward* ("the
plan document below", "the spec you will be given"), since it now precedes both
the plan and the assignment. `render_coder_prompt` then emits only the identity
block, the spec, and the verification items.

**One caveat worth deciding deliberately rather than by default:** the report
contract is an *output-format* constraint, and format instructions are followed
more reliably when they sit near the end of the prompt, immediately before the
model writes. Hoisting 100% of it to the front trades a known-good recency
position for a cache saving. The safer split is behavioural rules to the shared
prefix (they govern the whole session) while the per-group message keeps the exact
report block schema at its end — the invariant *prose* about when to use each
status can move forward, the literal block it must emit stays where it is. Worth
an A/B on report-block malformation rate before committing to the full hoist.

### R9 — inject the report contract at the moment the worker finishes, not at launch

R8's caveat dissolves if the contract does not have to be placed in advance at
all. A coder that started with a 40 KB shared context and worked to 200 KB has the
report contract sitting ~160 KB back; "near the end of the prompt" was true when
the fork message was written and is false by the time it matters. Delivering the
contract *when the worker declares it is done* puts it in the genuinely last
position, every time, regardless of how long the session ran.

There is already a seam for this, and it is currently wasted. `nudge_until_report`
(`sessions.py:717`) resumes the session when no valid report block is found, and
`_NUDGE_PROMPT` says:

    Your previous message did not end with a valid report block ({error}). Reply
    now with ONLY a <run-report status="..."> block whose body is valid JSON for
    the expected report schema — no other text.

It asks the worker to reproduce *"the expected report schema"* without including
it — demanding recall of a contract from 200 KB ago, at exactly the moment we have
evidence the worker has lost it. And the budget is thin: `DEFAULT_MAX_NUDGES = 2`,
so three attempts and the **whole round fails**. A round lost after 200 KB of real
work, purely because the closing 500 bytes were malformed, is the worst
cost/benefit in the system — the work is done, and only the communication failed.

**Put the expensive recovery entirely on the bad path.** The happy path stays
exactly as cheap as it is today — no extra turn, no extra tokens — and everything
below is spent only once a report has already failed to parse, where the
alternative is losing the round:

1. **The first nudge carries the full contract**, not a reference to it: the
   verbatim `report_contract`, the verification item ids that must appear, and the
   parse error. Zero happy-path cost, lands in the true last position, a few lines
   of code.
2. **The second nudge escalates instead of repeating.** Today both nudges send
   identical text — re-asking in the same words is not a strategy. The second
   should strip the task away entirely and present a filled-in skeleton: the exact
   block with the status attribute, every verification id pre-listed, and an
   instruction to emit only that with the values completed. At that point the
   worker is transcribing, not composing.

Together these make a communication-caused round failure very unlikely, and they
cost nothing on any round that reports correctly the first time — which is most of
them.

3. **A skill the worker invokes.** `/report` loads the contract on demand,
   worker-pulled and exactly positioned. Needs the plugin to ship the skill into
   every worktree and depends on skills being surfaced to a `claude -p` worker;
   adds a failure mode where the worker never invokes it.
4. **A tool instead of a prompt — the real end state.** Expose
   `submit_report(status, summary, verification_results, surprises, ...)` as an MCP
   tool. The JSON schema *is* the contract, enforced by the tool-call validator, so
   a malformed report becomes structurally impossible and `nudge_until_report`
   largely stops having a job. The report has always been an API call wearing prose
   clothes. Cost: an MCP server reachable from each worktree, and the confinement
   policy has to allow it — the same allowlist class of problem that produced the
   OAuth 401, so design it in rather than discovering it live.

Recommended: ship (1) and (2) now — they are small, they only ever run when
something has already gone wrong, and they convert most lost rounds into recovered
ones. Treat (4) as the target.

This also settles R8's caveat, from the other direction. Hoisting the contract into
the shared prefix might cost some format compliance; with (1) and (2) in place the
cost of that is a recovered nudge rather than a lost round, which is a price worth
paying for the cache saving. Under (4) the question disappears entirely — there is
no format instruction left to position.

