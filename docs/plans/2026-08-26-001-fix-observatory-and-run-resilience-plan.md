---
title: Observatory legibility and run resilience
type: fix
date: 2026-08-26
origin: docs/2026-08-20-observatory-frontend-findings.md
---

# Observatory legibility and run resilience

## Objective

Close every finding still open in `docs/2026-08-20-observatory-frontend-findings.md`
(F2–F27 plus R1–R9; F1 is already fixed and committed). Three outcomes, in
priority order:

1. **A run stops losing itself to causes no coder can fix.** A preflight failure
   is classified before blame is assigned, a re-auth 401 refreshes the token
   instead of halting thirteen groups, and a broadcast surprise no longer spends
   every group's rewrite budget.
2. **The Observatory stops lying.** A live run does not render as paused, a
   finished job does not render as running, a resumed run does not describe its
   original launch config, and the calibration table does not flatter the two
   estimates that were worst.
3. **The operator can see and steer what the run is doing.** Stage progress on a
   grouping job, addressable jobs, generation and group diffs, the speccer's LLM
   runs, model selection for all three roles, and a pre-filled resume form.

F9 records that `--name` already behaves correctly and needs no change; it is
listed here so its absence from the units is deliberate rather than an omission.

Not in scope: the drummAI-side dependency declarations (F15's `audio-separator`
fix, already landed there as `ffa19a8`, and the `demucs` defect F27 exposes) —
they belong to that repo.

## What we already know (resolved context)

Everything below was verified against the working tree during planning; workers
should not re-derive it.

**Already fixed, do not redo.**

- **F1** — `orchestrator/cli.py:1788` already calls `default_registry_path()`
  when `--registry` is omitted. Committed.
- **F13's root cause** — `orchestrator/execution/confinement.py:428` already
  grants `~/.claude/.credentials.json` read-write as a *single-file* rule
  (commit `c691dbd`), so a confined worker can write a refreshed OAuth token.
  This was the real cause of both 401 incidents. What does *not* exist is any
  401-specific handling: a `grep` for `401`/`OAuth`/`authenticat` across
  `orchestrator/execution/*.py` returns only that comment. A 401 is still a bare
  `SessionError`, which interrupts the group and, under `on_group_failure=halt`,
  sets `RUN_HALTED` (`orchestrator/execution/scheduler.py:155`).

**Auth mechanics (settles how U4 can work).**

- Confinement is **per-worker-session**: `SessionRunner.__init__` takes
  `confine: bool = True` (`orchestrator/execution/sessions.py:242`) and applies
  Landlock in `_spawn` via `preexec_fn` (`sessions.py:595-619`). The
  **orchestrator process itself is unconfined** and may therefore write
  `~/.claude/.credentials.json` directly.
- `~/.claude/.credentials.json` has the shape
  `{"claudeAiOauth": {"accessToken", "refreshToken", "expiresAt",
  "refreshTokenExpiresAt", "scopes", "subscriptionType", "rateLimitTier"},
  "organizationUuid"}`. `expiresAt` and `refreshTokenExpiresAt` are epoch
  milliseconds, so **expiry is checkable with no network call**.

**Cost accounting (settles U9).**

- `RoundUsage.from_envelope` (`sessions.py:150-163`) reads `iterations[-1]`.
  This is **correct for occupancy** and must not change: two tests pin it —
  `tests/test_sessions.py::test_multi_turn_envelope_reports_last_turn_context_not_the_round_sum`
  and its neighbour. Changing it re-opens the P0 where the breaker read a
  50x-inflated context and retired healthy coders.
- `SessionUsage.add` (`sessions.py:190-199`) then feeds those *same last-turn*
  numbers into `total_output_tokens` / `total_cache_read_tokens` /
  `total_cache_creation_tokens`, which `ui/src/components/CostPanel.tsx` renders
  as spend. That is the bug: a 190-turn round contributes one turn of spend.
- Research finding, external: per-request billing is independent, and
  `cache_read_input_tokens` on turn *n* reports the whole re-read prefix
  including what turn *n-1* wrote, billed at 0.1x. **Summing across turns is
  correct and is not double counting.** Better still, the envelope's *top-level*
  `usage` **already is** the all-turns sum (per the docstring at
  `sessions.py:151-159`), so spend needs no iteration walk at all and stays
  correct on older CLIs that emit no `iterations`.
- `LlmCallMeta.from_envelope` (`orchestrator/grouping/llm.py:72`) already reads
  top-level `usage`, so grouper cost accounting is already right. Leave it.

**Grouping reproducibility (settles U5–U7).**

- `index_fingerprint(status: dict)` (`orchestrator/grouping/graphing.py:60`)
  hashes the output of `codegraph status -j` — **operational counters, not
  content**. That is precisely why it churned three times in fifteen minutes at
  one commit while `sync` reported "already up to date".
- It is written once at `orchestrator/grouping/pipeline.py:498` into
  `ProvenanceEntry` (`orchestrator/grouping/trace.py:196-209`) and **never read
  back for comparison**.
- `CodegraphClient` (`graphing.py:118`) already exposes `sync()`, `status()`,
  `files_overview()`, `query()` and `_parsed()` — enough to build a canonical
  logical export cheaply.
- The partitioner is **already deterministic**: `_louvain`
  (`orchestrator/grouping/partition.py:511-545`) passes `seed=LOUVAIN_SEED`, and
  `partition.py:435` sorts rather than iterating a frozenset. Do not spend effort
  adding seeds.
- **Known residual, accepted:** the mapper is an LLM shelled through
  `orchestrator/grouping/llm.py` with no temperature or seed control, so a
  task→file/symbol mapping can differ against a byte-identical index. Pinning the
  index makes grouping *index-stable*, not *reproducible*. Content-addressing the
  mapper output was considered and deferred (see Decisions).

**Preflight and the merge gate (settles U1–U3).**

- `run_preflight` (`orchestrator/execution/preflight.py:56`) records only
  `returncode != 0` plus a combined log at `preflight-check.log`, and raises
  `PreflightFailure` (`preflight.py:29`), which already carries `reason` and
  `output_path`. `detect_check_command` (`preflight.py:43`) resolves
  `uv run pytest` with no `--junitxml`.
- pytest exit codes are a designed triage signal: `1` tests ran and some failed,
  `2` interrupted/collection error, `3` internal error, `4` usage error, `5` no
  tests collected. An `ImportError`/`ModuleNotFoundError` during **collection**
  means zero tests ran and is categorically not evidence about the diff — the
  exact shape of the incident that burned three generations.
- The consumer at `orchestrator/execution/review.py:731-748` unconditionally
  does `_spread(surprise)` → `_escalate(MERGE_CONFLICT)` → `_rewrite(...)`, and
  `_rewrite` is what advances the generation counter toward `max_generations`
  (`review.py:815`, `review.py:829`). Note it reuses
  `EscalationKind.MERGE_CONFLICT` even for a preflight failure.
- `preflight.py:88`'s `declared_files` reporting is deliberately non-blocking
  with a documented rationale. Leave it non-blocking.
- `_dirty_paths` (`preflight.py:130`) requires a clean worktree, so pytest's own
  `.pytest_cache` must not be relied on — use `-p no:cacheprovider` and drive
  comparison from a JUnit XML written outside the worktree.

**Surprises and rewrites (settles U11–U14).**

- `SurpriseBoard.mark` (`review.py:126-131`) does
  `self._pending.setdefault(gid, []).append(surprise)` for every id in
  `affected_groups`, with **no check that the id names a group in this run**. At
  the end of the observed run, 24 surprises sat undelivered across seven buckets:
  17 under *task* ids (`u16-play-route`, `u10-calibration-passes`), 6 under group
  ids that do not exist (`g14`–`g17` in a 13-group run), 3 under a real group
  that had already completed.
- The valid id sets are already on disk: `llm/calls.json` records `task_ids` and
  `group_ids` under `produced`.
- `Surprise.affected_groups` is `list[str]` (`orchestrator/model.py:137`);
  producers are `merge.py:120`, `merge.py:145`, `review.py:714`, `review.py:738`,
  `_context_surprise` (`review.py:1044`) and `_operator_surprise`
  (`review.py:1050`).
- `_rewrite` sets `GroupState.REWRITING`, calls `deps.rewrite_spec(...)` and does
  `self.rewrites += 1`, and **never calls `self._log`** — `grep -c surprise
  logs/run.log` over the whole run returns 0. `execution.max_rewrites` is 2
  (`orchestrator/config.py:273`). The rewritten spec is never persisted and the
  rewrite speccer call never reaches `llm/calls.json`.
- Delivery points are `review.py:219` (before launch), `review.py:374` (a
  surprise vetoes a pending approval), and every failure path.

**Prompting (settles U15–U16).**

- `base-context.md` is **generated**, not a static prompt file: it is compiled by
  `compile_base_context(repo_root, plan_path, codegraph_summary)`
  (`orchestrator/grouping/base_context.py:18`), called from
  `orchestrator/grouping/pipeline.py:412`, written at `cli.py:618`, and loaded
  into the base session by `SessionRunner.start_base` (`sessions.py:296-308`).
  Workers are **forks** of that session (`review.py:300-302`, `start_fork`), so
  the base context is a shared cache-warm prefix across all forks.
- `render_coder_prompt` (`orchestrator/execution/prompting.py:51`) substitutes
  `$identity_block`, `$verification` and `$report_contract` into
  `orchestrator/prompts/coder.md`; `$report_contract` sits **last**
  (`coder.md:44`), i.e. the layout is already recency-favourable.
- The invariant text is `coder.md` (2 567 B) + `report_contract.md` (2 341 B) +
  `reviewer.md`; the observed run forked 17 coders and 12 reviewers, so roughly
  106 KB of byte-identical instruction text was re-sent as fresh per-fork input.
- `nudge_until_report` (`sessions.py:717`) asks the worker to reproduce "the
  expected report schema" **without including it**, and both nudges send
  identical text. `DEFAULT_MAX_NUDGES = 2`, so three failures lose the whole
  round after all the real work is already committed.
- Research finding, external: format instructions at the front of a ~200 KB
  context are followed less reliably than the same instructions immediately
  before generation. The mitigation is a hybrid — bulk in the cached prefix, a
  compact tail restatement per fork.

**Model selection (settles U17–U18).**

- `SessionConfig.model` defaults to `None` and `sessions.py` only appends
  `--model` when set. No CLI flag exposes it (`grep '"--model"'
  orchestrator/cli.py` finds nothing), and `orchestrator/grouping/llm.py` builds
  its own argv with no model flag at all.
- Measured on the observed run: speccer `claude-opus-5`
  (`groupings/…/llm/calls.json`, `gen_ai.request.model`), coders `claude-opus-5`
  (388 assistant turns across three worktree transcripts). **Opus everywhere.**

**Observatory internals (settles U19–U31).**

- `RunManifest(...)` / `store.save(manifest)` sit inside the branch that
  establishes a base session (`cli.py:1182`, `cli.py:1191`). A resume reuses the
  existing base session and skips that block, so the manifest keeps the *first*
  launch's `escalation` and `usage_limit` — and `/snapshot` serves the manifest.
- `UsageLimitState.released_at` (`orchestrator/execution/ratelimit.py:159`) is
  written only by `_release_locked` (`ratelimit.py:379`) in the process that
  armed the pause. That process died while paused, so the record is stuck armed
  forever; `UsageLimitBanner` computes `paused = Boolean(usageLimit &&
  !usageLimit.released_at)` and shows a live run as paused.
- `GroupHeartbeat` (`orchestrator/observatory/runs.py:88`) carries
  `phase_elapsed_s` but omits `paused_s` and `round_elapsed_s`, both of which are
  on disk in `heartbeat.json`.
- `orchestrator/observatory/launch.py` already has `ExecutionOptions`
  (`launch.py:89`), `GroupJobBody` (`launch.py:134`), `JobInfo` (`launch.py:194`),
  `list_jobs` (`launch.py:356`) and `read_job` (`launch.py:342`);
  `GET /api/projects/{p}/jobs` already returns what a jobs view needs. The job
  record keeps its launch `options` at `.orchestrator/jobs/<id>/command.json`.
- `ui/src/routes.tsx` has no `/p/:project/jobs` route. `ui/src/routes/Launch.tsx`
  fetches once in a `useEffect` keyed on `[project]` (`Launch.tsx:46`) and passes
  only `dry_run` (`Launch.tsx:188`).
- `IntegrationMerger.ensure()` calls `create_worktree` and stops; `provision_env`
  has exactly one call site, inside `workspace_for` (`cli.py:1359`), which runs
  only for **group** worktrees. Group worktrees get
  `uv sync --all-extras` (`ExecutionConfig.provision_args`); the integration
  worktree gets nothing, and nothing says so.
- `uvicorn` is imported by `_cmd_ui` but declared nowhere in `pyproject.toml`; it
  arrives transitively via fastmcp.
- `finish.py`'s `_VERDICT_RE` (`^verdict-g(\d+)-r(\d+)\.json$`) deliberately does
  not match `-extra.json`, so a `paired_plus` group's PR body reports the
  first-pass verdict. Benign today; worth labelling rather than changing.

## Decisions

- **Preflight triage is baseline-first, with the orchestrator as the
  classifier.** A cheap deterministic ladder runs first — pytest exit code plus
  collection-error detection, then a per-test diff against a recorded baseline —
  and only a failure that survives both rungs is classified. The classifier is
  the **orchestrator's own session**, not a separate cheap model, because it
  already holds the plan, the codegraph summary and the group's spec, and because
  its output is wanted anyway: the *diagnosis* it writes is what the next
  generation is told. *Alternatives rejected:* an LLM-only classifier (adds a call
  on every failure path and a new source of wrong verdicts, with no baseline to
  check itself against); baseline-only (a new-but-environmental failure — a dep
  that broke mid-run, a flake — still burns generations).
- **F18 is folded into F16 rather than shipped separately.** Attaching the
  `short test summary info` tail to the surprise and having the orchestrator write
  a diagnosis are the same change to the same code path; splitting them would put
  two units on one function.
- **A 401 tries to fix itself before it asks for a human.** Three rungs: read
  `expiresAt` from the credentials file after any long pause (no network call);
  if stale, have the *unconfined orchestrator* trigger a refresh; only if
  `refreshTokenExpiresAt` has also passed, or the refresh still 401s, arm a pause
  and notify. *Rationale:* the first two incidents were a token that could not be
  written, not a login that had lapsed — an operator prompt would have been the
  wrong ask. *Alternatives rejected:* pause-and-notify only (correct but asks the
  operator for something they mostly cannot supply); demote to group-level failure
  (every running group hits the same 401 and they all fail one by one).
- **The index fingerprint becomes a content hash, and drift is prevented rather
  than merely detected.** A canonical logical export (sorted symbols, files,
  edges) replaces the `status -j` counter hash, and a quiescence handshake polls
  until it is stable across N reads before partitioning. Comparison hard-fails
  only on the **resume/reuse** path; a fresh `group` legitimately sees a new
  index and must not be blocked. *Alternatives rejected:* detect-and-warn only
  (leaves the drift unexplained and the partition still moving); instrument-first
  (the seven candidate mechanisms all have the same fix, so the empirical check
  is worth folding into the work, not gating it).
- **Mapper output is not content-addressed in this plan.** It is the honest
  remaining gap in reproducibility and it is a real caching layer keyed on
  `(plan sha, index fingerprint, prompt version, model)`. Recorded as a known
  residual instead, so nobody reads "index-stable" as "reproducible".
  *Alternative rejected:* building it now — it doubles this slice and the index
  fix delivers most of the value alone.
- **Spend and occupancy become two quantities, and `from_envelope` is not
  touched.** Spend reads the envelope's top-level `usage`; occupancy keeps
  `iterations[-1]`. *Rationale:* top-level `usage` already is the all-turns sum,
  so this is simpler than an iteration walk and degrades correctly on old CLIs.
  *Alternatives rejected:* summing inside `from_envelope` (re-opens the retired-
  healthy-coders P0); a model price table and dollar figures (the two sources
  disagree on whether the envelope exposes the per-TTL cache-creation split, so
  a cost formula would be built on an unverified assumption — token classes are
  reported instead, and the probe is recorded as a verification item).
- **The calibration table labels, reports the peak, and excludes.** Every row
  carries `generations` and `retirement_reason` and shows both last-generation
  and peak occupancy; the median and aggregate are computed over single-generation
  rows only. *Rationale:* the panel exists for manual reading, so hiding a row is
  worse than labelling it, but a summary over a mixed population is meaningless.
  *Alternative rejected:* peak-only (loses the fact that a retirement happened).
- **A broadcast surprise gets a kind that costs no rewrite; fan-out is not
  capped.** A new informational kind briefs the next generation without
  incrementing `rewrites` or triggering a speccer call. A surprise naming more
  than N groups is **logged as a warning** at mark time rather than truncated.
  *Rationale:* the informational kind is the precise fix for the budget drain, so
  a cap would only bite a rewrite-worthy broad note — where dropping it silently
  is the worse failure. *Alternative rejected:* capping fan-out.
- **Ground rules hoist; the report block's tail restatement stays.** Behavioural
  invariants move into the compiled base context (paid once, read from cache by
  every fork); the per-fork message keeps a compact tail naming the exact tag, the
  permitted `status` values and "exactly one block, valid JSON, nothing after it".
  *Alternatives rejected:* migrating the report to `--json-schema` (strongest fix
  and the right end state, but it couples to a CLI flag, requires the schema be
  pinned per run because a change invalidates every fork's cached prefix, and
  needs the tag parser kept as a fallback for resumed rounds — a plan of its own);
  nudges-only with no hoist (forgoes ~106 KB of re-sent text for a compliance risk
  the tail restatement already covers).
- **Nudge escalation is put entirely on the bad path.** Nudge 1 carries the
  verbatim contract plus the parse error; nudge 2 sends a filled-in skeleton to
  transcribe. Zero happy-path cost. *Rationale:* losing a round after 200 KB of
  committed work because the closing 500 bytes were malformed is the worst
  cost/benefit in the system.
- **Model defaults change: workers Sonnet, orchestrator and speccer Opus.** Three
  independently settable knobs, plumbed config → CLI → form. *Rationale:* workers
  are the bulk of spend and the bulk of their work is mechanical; the speccer is
  one call per grouping and is the place the strongest model earns its cost.
  *Alternatives rejected:* one worker-only knob (leaves the speccer unselectable);
  keeping inherit-everywhere as the default (the measured answer was Opus
  everywhere, which is what the session limits were paying for).
- **The integration worktree is provisioned like a group worktree.** Same
  `provision_args`, at `ensure()`, and every worktree's provisioning is logged
  with its exact `uv sync` invocation. *Rationale:* it is the tree that represents
  the run's output and an operator naturally goes there to run what was built.
- **The reference documentation is updated as part of the work, not after.**
  Several of these are structural changes to documented behaviour (failure
  classification, surprise kinds, model selection, worktree provisioning), so
  `docs/orchestrator-reference.html` and the base docs are a unit with explicit
  dependencies on the units that change the behaviour they describe.

- **The task map lists no symbols, and the plan says why.** `symbols` is
  optional in the map contract, and in this codebase filling it is actively
  harmful: the derived call/impact edges it produces run through `cli.py`
  (75 KB) and `sessions.py`, which every layer already touches, and they
  saturated the task graph into a single 25-task SCC that cycle repair could not
  re-split. Files alone carry the affinity the partitioner needs here. Each
  unit's exact symbols are named in *What we already know* and in the unit prose
  instead, where a worker reads them and the graph does not.
  *Alternative rejected:* keeping symbols and accepting a degenerate partition
  via `--allow-degenerate-partition`, which would hand one worker most of the
  plan.
- **Most units carry no slice, because the slice check prices work at 2.5x.**
  The slice-overflow check multiplies each member's node work by
  `coder_slack_multiplier` (2.5) before comparing to the ~122k cap, while the
  per-group listing reports the unmultiplied figure — the same unit reads 34,503
  in one output and 138,193 in the other. In those slice units a task touching
  `orchestrator/cli.py` (75,040 bytes → 18,760 tokens raw → 26,388 node work)
  already spends ~66k of the cap, so a cross-layer slice pairing two
  `cli.py`-touching tasks cannot fit by construction. Slices were
  kept only where the members are small and genuinely one testable feature
  (`repro`, `accounting`, `prompting`, `surprises`, `jobs`, `legibility`);
  everywhere else ordering is carried by `depends_on` alone. *Alternative
  rejected:* forcing slices and running with `--allow-oversized-slice`, which
  keeps an over-budget slice whole as one flagged group — the failure this plan
  is partly trying to stop causing.

No decision here met all three ADR bars (hard to reverse, surprising without
context, a real trade-off) — each is either cheaply reversible or a
straightforward reading of the evidence in the findings doc. No ADRs promoted.

## Units

### U1. preflight-classification — give a preflight failure a typed cause instead of a bare exit code

- **Goal**: `run_preflight` classifies before it raises. `PreflightFailure` gains
  a `kind` field with values `env`, `timeout`, `regression`; `env` covers exit
  codes 2/3/4/5 and exit 1 whose output shows a collection-phase `ImportError` /
  `ModuleNotFoundError` / `ImportError while loading conftest.py`; `timeout` is
  the existing `TimeoutExpired` path; `regression` is exit 1 with tests actually
  run. `detect_check_command` adds `--junitxml` writing **outside** the worktree
  (and `-p no:cacheprovider`) so the clean-tree requirement still holds.
- **Files**: `orchestrator/execution/preflight.py`, `tests/test_preflight.py`
- **Symbols**: —
  `_dirty_paths`
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `preflight-failure-kind`
- **Verification**:
  - A check command exiting 2 raises `PreflightFailure` whose `kind` is `env`.
  - A check command exiting 1 whose captured output contains
    `ImportError while loading conftest.py` raises `kind == "env"`.
  - A check command exiting 1 with an ordinary assertion failure and no
    collection error raises `kind == "regression"`.
  - A check command that times out raises `kind == "timeout"`.
  - After a preflight run, `git status --porcelain` inside the worktree is empty
    — no `.pytest_cache` and no JUnit XML were written into it.
  - The JUnit XML exists at the path outside the worktree that `run_preflight`
    reports.
  - `declared_files` reporting remains non-blocking: a group that declared a file
    it never created still reaches the merge attempt.

### U2. preflight-baseline — record what was already red before the run started

- **Goal**: at run start the check command is executed once on the launch branch
  and its per-test outcome set is persisted to the run directory as
  `preflight-baseline.json` (test ids, outcomes, exit code, the command, the
  commit sha). A helper compares a group's failing-test set against it and
  returns the **new** failures. An identical failure set on baseline and head is
  an explicit "pre-existing, not attributable" verdict, not an inconclusive one.
  A baseline that could not be captured is recorded as absent and degrades to
  "no baseline" rather than to a false attribution.
- **Files**: `orchestrator/execution/preflight.py`,
  `orchestrator/cli.py`,
  `tests/test_preflight_baseline.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: u1-preflight-classification
- **Slice**: —
- **Implements / Consumes**: implements `preflight-baseline`;
  consumes `preflight-failure-kind`
- **Verification**:
  - Starting a run writes `preflight-baseline.json` into the run directory
    containing the check command, the commit sha and one entry per failing test.
  - Given a baseline with tests A and B failing and a head result with A, B and C
    failing, the comparison returns exactly `{C}`.
  - Given a baseline and a head result with identical failing sets, the
    comparison returns an empty new-failure set and a `pre_existing` verdict.
  - When no baseline could be captured, the comparison returns a `no_baseline`
    verdict and never reports a failure as new.
  - A run whose check command passes cleanly at start records an empty failing
    set, not an absent baseline.

### U3. merge-gate-triage — route a failure by its cause, and tell the next generation what actually broke

- **Goal**: the merge gate stops treating every failure identically. A failure
  classified `env`/`timeout`, or one whose failing tests are all pre-existing per
  the baseline, does **not** call `_rewrite` — it fails the group fast with the
  diagnosis attached, or escalates to the operator under a preflight-specific
  escalation kind rather than reusing `MERGE_CONFLICT`. A failure that is new and
  attributable keeps today's rewrite-and-respawn behaviour. When classification
  is needed beyond the deterministic rungs, the **orchestrator's own session**
  performs it, and its written diagnosis — together with the `short test summary
  info` tail from the check output — is what the surprise carries, so generation
  *n+1* opens with the actual error rather than a file path (F18).
- **Files**: `orchestrator/execution/review.py`,
  `orchestrator/execution/merge.py`,
  `tests/test_merge_gate_triage.py` *(new, large)*
- **Symbols**: —
  `_advance_generation`, `MergeConflict`, `SurpriseBoard`
- **Depends-on**: u1-preflight-classification, u2-preflight-baseline
- **Slice**: —
- **Implements / Consumes**: consumes `preflight-failure-kind`,
  `preflight-baseline`
- **Verification**:
  - A preflight failure with `kind == "env"` leaves the group's generation
    counter unchanged and its rewrite count unchanged.
  - A preflight failure whose failing tests are all present in the baseline
    leaves the generation counter unchanged.
  - A preflight failure that is new and attributable increments the generation
    counter exactly once, as today.
  - The escalation raised for a preflight failure reports a preflight-specific
    kind, distinguishable in the escalation record from a git merge conflict.
  - The surprise handed to the next generation contains the `short test summary
    info` lines from the check output, not only the path to
    `preflight-check.log`.
  - A group failed on an `env` verdict reports a diagnosis string naming the
    cause in its run state, readable without opening the check log.

### U4. auth-refresh-ladder — refresh an expired token instead of halting thirteen groups

- **Goal**: a three-rung ladder. (a) After any pause longer than a threshold, and
  before the first retry, the orchestrator reads `expiresAt` from
  `~/.claude/.credentials.json` — no network call — and treats a stale token as
  expected rather than exceptional. (b) If stale, the **unconfined orchestrator
  process** triggers a refresh itself and re-reads the file to confirm
  `expiresAt` advanced. (c) If `refreshTokenExpiresAt` has also passed, or the
  refresh does not advance `expiresAt`, or a live call still returns
  `401 … Re-authenticate`, the run arms a pause record and notifies rather than
  halting — re-probing periodically and clearing itself once the credential is
  healthy. A 401 is no longer a bare `SessionError`.
- **Files**: `orchestrator/execution/auth.py` *(new, medium)*,
  `orchestrator/execution/sessions.py`,
  `orchestrator/execution/ratelimit.py`,
  `tests/test_auth_ladder.py` *(new, medium)*
- **Symbols**: —
  `UsageLimitState`, `SessionRunner`
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `auth-pause`
- **Verification**:
  - Given a credentials file whose `expiresAt` is in the past, the expiry check
    reports the token stale without making any network call.
  - Given a credentials file whose `expiresAt` is in the future, the expiry check
    reports it healthy and no refresh is attempted.
  - After a successful refresh, the ladder confirms `expiresAt` advanced and the
    run proceeds without arming a pause.
  - Given a credentials file whose `refreshTokenExpiresAt` is also past, the
    ladder skips the refresh attempt and arms a pause directly.
  - A `401 … Re-authenticate to continue` surfaced after the refresh path arms a
    pause record and leaves the scheduler admitting groups, rather than setting
    `RUN_HALTED`.
  - Once the credential is healthy again, the armed auth pause releases itself on
    the next probe and the paused groups resume.

### U5. index-fingerprint-content — hash what the index contains, not how it is feeling

- **Goal**: `index_fingerprint` is computed from a canonical **logical export** —
  sorted symbol ids, sorted file paths, sorted edges, canonically serialised,
  then sha256'd — instead of hashing `codegraph status -j`'s operational
  counters. The `ProvenanceEntry` gains `LOUVAIN_SEED` and `louvain_resolution`
  so the recorded partition key is complete.
- **Files**: `orchestrator/grouping/graphing.py`,
  `orchestrator/grouping/trace.py`,
  `tests/test_index_fingerprint.py` *(new, medium)*
- **Symbols**: —
  `status`, `ProvenanceEntry`, `LOUVAIN_SEED`
- **Depends-on**: —
- **Slice**: repro
- **Implements / Consumes**: implements `index-fingerprint-v2`
- **Verification**:
  - Two fingerprint computations over the same logical export produce the same
    string, and the string is stable across process restarts.
  - Changing only an operational counter in `codegraph status -j` (queue depth,
    uptime, cache size) leaves the fingerprint unchanged.
  - Adding one symbol to the logical export changes the fingerprint.
  - Reordering the export's symbols, files or edges leaves the fingerprint
    unchanged.
  - A recorded `ProvenanceEntry` includes the Louvain seed and resolution
    alongside the fingerprint.

### U6. index-quiescence — do not partition against a moving index

- **Goal**: after `CodegraphClient.sync()`, poll the fingerprint until it is
  identical across N consecutive reads separated by a short interval, under a
  timeout. A timeout **fails the grouping** with a message naming the observed
  fingerprints rather than proceeding on a moving index. Each observed
  fingerprint and the full `status -j` payload behind it are recorded, so the
  drift mechanism is diagnosable from a single run rather than needing a separate
  investigation.
- **Files**: `orchestrator/grouping/graphing.py`,
  `orchestrator/grouping/pipeline.py`,
  `tests/test_index_quiescence.py` *(new, medium)*
- **Symbols**: —
  `GraphBuildError`
- **Depends-on**: u5-index-fingerprint-content
- **Slice**: repro
- **Implements / Consumes**: consumes `index-fingerprint-v2`
- **Verification**:
  - Given a client whose fingerprint is stable, the handshake returns after the
    minimum number of reads and the grouping proceeds.
  - Given a client whose fingerprint changes on every read, the handshake raises
    `GraphBuildError` naming the distinct fingerprints observed.
  - Given a client whose fingerprint settles after two changes, the handshake
    returns the settled value and the grouping proceeds.
  - The recorded trace holds every observed fingerprint and its `status -j`
    payload, so a drift can be attributed after the fact.

### U7. fingerprint-compare-on-resume — refuse to reuse a partition built against a different index

- **Goal**: the recorded fingerprint is read back and compared. On the
  **resume/reuse** path a mismatch is a hard failure naming both fingerprints and
  the fix; `--allow-index-drift` downgrades it to a loud warning and forces a
  re-partition, never a silent reuse. A **fresh** `group` invocation never fails
  on mismatch. The known residual — that the mapper is an LLM with no seed, so
  this delivers index-stability rather than reproducibility — is stated in the
  warning text and in the docs.
- **Files**: `orchestrator/grouping/pipeline.py`,
  `orchestrator/cli.py`,
  `tests/test_fingerprint_compare.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: u5-index-fingerprint-content
- **Slice**: —
- **Implements / Consumes**: consumes `index-fingerprint-v2`
- **Verification**:
  - Resuming a run whose grouping recorded fingerprint X against a current index
    of fingerprint Y exits non-zero with a message containing both X and Y.
  - The same resume with `--allow-index-drift` proceeds, prints a warning naming
    both fingerprints, and re-partitions rather than reusing the recorded
    partition.
  - A fresh `group` invocation against an index whose fingerprint differs from
    any previous grouping completes normally with no mismatch error.
  - Resuming a run whose recorded fingerprint matches the current index proceeds
    silently.

### U8. specless-preview-dir — stop `--no-spec` corrupting a named grouping directory

- **Goal**: `--no-spec` and `--dry-run` write their `grouping-trace.json` and
  `edge-provenance.json` into a preview location (`groupings/<name>/preview/`)
  rather than beside a `groups.json` describing a different partition. A specless
  run into a directory holding a `groups.json` from a different partition never
  overwrites or shadows it, and a directory that has only ever seen a specless run
  is not mistaken for a failed grouping by `describe_groupings`.
- **Files**: `orchestrator/cli.py`,
  `orchestrator/grouping/pipeline.py`,
  `tests/test_specless_preview.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - Running `group --no-spec --name N` after a real grouping named N leaves the
    existing `groups.json` and its sibling trace untouched, and writes the new
    trace under the preview location.
  - The Observatory's groupings listing for N reports the partition in
    `groups.json`, not the one in the preview trace.
  - Running `group --no-spec --name N` before any real grouping of N leaves N
    absent from `describe_groupings` output, or listed explicitly as a preview —
    never as a failed grouping.
  - A subsequent real grouping of N writes `groups.json` normally and its trace
    sits beside it.

### U9. spend-vs-occupancy — count spend over every turn, keep occupancy at the last

- **Goal**: spend and occupancy become two quantities. A new spend path reads the
  envelope's **top-level** `usage` (already the all-turns sum) and feeds
  `SessionUsage`'s `total_output_tokens` / `total_cache_read_tokens` /
  `total_cache_creation_tokens`. `RoundUsage.from_envelope` and
  `last_context_tokens` are **unchanged**. Turn 1's inherited cache read — context
  the session did not create and cannot shrink — is reported separately.
  `CostPanel` renders the corrected token classes. One real envelope is inspected
  to record whether a per-TTL cache-creation split
  (`ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`) is present, and the
  answer is written into the docs — no dollar figures are computed in this unit.
- **Files**: `orchestrator/execution/sessions.py`,
  `orchestrator/model.py`,
  `ui/src/components/CostPanel.tsx`,
  `tests/test_session_spend.py` *(new, medium)*
- **Symbols**: —
  `add`, `SessionEntry`
- **Depends-on**: —
- **Slice**: accounting
- **Implements / Consumes**: implements `session-spend`
- **Verification**:
  - Given a 190-turn envelope, the recorded `total_output_tokens` equals the
    top-level `usage` output figure, not the final iteration's.
  - Given the same envelope, `last_context_tokens` still equals the final
    iteration's context, and the two existing tests pinning last-turn occupancy
    still pass unmodified.
  - Given an envelope with no `iterations` key, spend still reports the top-level
    `usage` figures and occupancy still falls back as it does today.
  - The turn-1 inherited cache read is reported as its own figure, distinct from
    total cache read.
  - `CostPanel` renders a cache-creation figure that scales with the number of
    turns in the round rather than staying constant.
  - The docs record, from a real envelope, whether the per-TTL cache-creation
    split is present in the payload.

### U10. calibration-generations — stop the calibration table flattering its worst estimates

- **Goal**: every calibration row carries `generations` and `retirement_reason`,
  and reports **both** the last-generation occupancy and the peak occupancy
  across generations. Rows with more than one generation are labelled as such and
  **excluded from the median and aggregate**, which are computed over
  single-generation rows only and say so. Rows stay visible in the table.
- **Files**: `ui/src/components/CostPanel.tsx`,
  `ui/src/components/CostPanel.test.tsx`,
  `orchestrator/observatory/runs.py`
- **Symbols**: —
- **Depends-on**: u9-spend-vs-occupancy
- **Slice**: accounting
- **Implements / Consumes**: consumes `session-spend`
- **Verification**:
  - A group with one coder generation shows one ratio and is included in the
    median.
  - A group with four generations shows both the last-generation ratio and the
    peak ratio, is labelled with its generation count and the reason the earlier
    generation was retired, and is excluded from the median.
  - The summary line states the population it was computed over (single-generation
    rows only) and the count of rows excluded.
  - A run in which every group is multi-generation renders a summary that reports
    no median rather than a median over zero rows.

### U11. surprise-validation — a surprise addressed to nobody is reported, not silently dropped

- **Goal**: `SurpriseBoard.mark` validates each id in `affected_groups` against
  the run's real group ids. An id that names no group is logged at mark time
  with the id, the source group and the surprise, and routed to the run-level
  surprise list rather than a dead bucket — including the common case of a
  **task id** from the plan (`u16-play-route`), which is resolved to its owning
  group where possible. A surprise naming more than N groups is logged as a
  wide-fan-out warning (not truncated). Identical surprises re-emitted by
  successive rounds and generations are deduplicated within a bucket.
- **Files**: `orchestrator/execution/review.py`,
  `tests/test_surprise_board.py` *(new, medium)*
- **Symbols**: —
  `Surprise`, `_context_surprise`, `_operator_surprise`
- **Depends-on**: —
- **Slice**: surprises
- **Implements / Consumes**: implements `surprise-validation`
- **Verification**:
  - A surprise naming `g14` in a 13-group run appears in the run-level surprise
    list and produces a log line naming `g14` and the source group.
  - A surprise naming the task id `u16-play-route` is delivered to the group that
    owns that task, and no `u16-play-route` bucket is created.
  - A surprise naming a task id owned by no group in the run lands in the
    run-level list with a log line.
  - A surprise naming 16 groups in a 13-group run is still delivered to the 13
    that exist and produces a wide-fan-out warning naming the count.
  - The same surprise marked five times against one group is delivered once.
  - A surprise naming a valid, still-running group is delivered unchanged.

### U12. surprise-residue-report — say what never got delivered

- **Goal**: the end-of-run summary and `finish` list everything still pending on
  the board, by bucket, each with a stated reason: "never delivered — group
  already completed", "unknown group id", "run ended before delivery". Today this
  requires reading `surprises.json` by hand to know it exists at all. The
  Observatory gains a surprise view showing what is pending *for* a group, not
  only what the group emitted.
- **Files**: `orchestrator/execution/finish.py`,
  `orchestrator/execution/review.py`,
  `orchestrator/observatory/runs.py`,
  `ui/src/components/SurpriseBoard.tsx` *(new, medium)*,
  `ui/src/components/GroupDrillIn.tsx`
- **Symbols**: —
  `RunSnapshot`
- **Depends-on**: u11-surprise-validation
- **Slice**: —
- **Implements / Consumes**: consumes `surprise-validation`
- **Verification**:
  - A run ending with pending surprises prints a residue section listing each
    bucket, its entry count, and a reason per bucket.
  - A run ending with an empty board prints no residue section, or an explicit
    "none pending".
  - A surprise pending for a group that completed before it was written is
    reported with the "group already completed" reason.
  - The group drill-in shows surprises pending *for* the group separately from
    surprises the group emitted.

### U13. informational-surprise-kind — brief the next generation without spending its rewrite budget

- **Goal**: a new informational surprise kind that is folded into the next
  generation's briefing but does **not** increment `rewrites` and does **not**
  trigger a speccer call. Broadcast facts — a changed test baseline, a
  pre-existing red suite — use it. The rewrite-consuming kinds keep today's
  behaviour, so a group that later hits a genuine merge conflict and a genuine
  preflight failure still has both its rewrites.
- **Files**: `orchestrator/model.py`,
  `orchestrator/execution/review.py`,
  `tests/test_informational_surprise.py` *(new, medium)*
- **Symbols**: —
  `_spread`
- **Depends-on**: —
- **Slice**: surprises
- **Implements / Consumes**: implements `informational-surprise`
- **Verification**:
  - A group consuming only informational surprises has an unchanged `rewrites`
    count and triggers no speccer call.
  - The informational surprise's text still reaches the next generation's
    briefing.
  - A group consuming one informational and one rewrite-consuming surprise
    increments `rewrites` exactly once.
  - A group that consumed sixteen informational surprises can still survive two
    subsequent rewrite-consuming failures before exhausting its budget.

### U14. rewrite-observability — make the most consequential mid-run event legible

- **Goal**: `_rewrite` logs to `run.log` — the group, the generation, the
  surprises consumed and why; the rewritten spec is persisted to the group
  directory as `spec-gen<N>.json` so a post-mortem can reconstruct what the coder
  was actually told; and the rewrite speccer call is appended to the run's
  `llm/calls.json` so its cost and its prompt/response survive.
- **Files**: `orchestrator/execution/review.py`,
  `orchestrator/cli.py`,
  `tests/test_rewrite_observability.py` *(new, medium)*
- **Symbols**: —
  `LlmCallMeta`
- **Depends-on**: u13-informational-surprise-kind
- **Slice**: —
- **Implements / Consumes**: consumes `informational-surprise`
- **Verification**:
  - After a run in which any group was re-specced, `grep -c surprise
    logs/run.log` is greater than zero.
  - A rewritten group's directory contains `spec-gen2.json` whose contents differ
    from the group's entry in `groups.json`.
  - The run's `llm/calls.json` contains one entry per rewrite speccer call, each
    with its token usage and model.
  - A group that was never re-specced writes no `spec-gen*.json` file.

### U15. hoist-ground-rules — pay the invariant instructions once, not twenty-nine times

- **Goal**: the worker ground rules and the behavioural prose of the report
  contract move into the compiled base context, ahead of the plan document and
  the codegraph summary, rewritten to refer *forward* ("the plan document below",
  "the spec you will be given"). `render_coder_prompt` and
  `render_reviewer_prompt` then emit the identity block, the spec, the
  verification items, and a compact **tail restatement** naming the exact tag,
  the permitted `status` values, and "exactly one block, valid JSON, nothing
  after it". The literal report block schema stays at the end of the per-fork
  message.
- **Files**: `orchestrator/grouping/base_context.py`,
  `orchestrator/execution/prompting.py`,
  `orchestrator/prompts/coder.md`,
  `orchestrator/prompts/reviewer.md`,
  `orchestrator/prompts/report_contract.md`,
  `orchestrator/prompts/worker_ground_rules.md` *(new, medium)*
- **Symbols**: —
  `render_reviewer_prompt`, `render_identity`, `_verification_lines`
- **Depends-on**: —
- **Slice**: prompting
- **Implements / Consumes**: implements `worker-ground-rules`
- **Verification**:
  - The compiled base context contains the worker ground rules and they appear
    before the plan document and the codegraph summary.
  - A rendered coder prompt is at least 2 000 bytes smaller than before the
    change for the same group.
  - A rendered coder prompt still ends with the report block schema and a tail
    restatement naming the tag and the permitted status values.
  - A rendered coder prompt no longer contains the worktree-confinement or
    commit-early-and-often prose.
  - The ground rules text in the base context refers forward to the plan and the
    spec, containing no backward reference to material that now follows it.
  - A rendered reviewer prompt is likewise reduced and still carries its own tail
    restatement.

### U16. smart-nudges — spend the recovery effort only after a report has already failed

- **Goal**: `nudge_until_report` stops asking the worker to recall a contract
  from 200 KB ago. Nudge 1 carries the **verbatim** report contract, the
  verification item ids that must appear, and the parse error. Nudge 2 escalates
  instead of repeating: it strips the task away and presents a filled-in skeleton
  — the exact block with the status attribute and every verification id
  pre-listed — asking only that the values be completed. Neither costs anything
  on a round that reports correctly the first time.
- **Files**: `orchestrator/execution/sessions.py`,
  `orchestrator/execution/prompting.py`,
  `tests/test_nudges.py` *(new, medium)*
- **Symbols**: —
  `render_coder_prompt`
- **Depends-on**: u15-hoist-ground-rules
- **Slice**: prompting
- **Implements / Consumes**: consumes `worker-ground-rules`
- **Verification**:
  - A round whose first response parses cleanly sends no nudge and adds no turn.
  - The first nudge's text contains the full report contract and the parse error
    message.
  - The first nudge's text lists every verification item id the report must
    carry.
  - The second nudge's text differs from the first and contains a skeleton block
    with the status attribute and every verification id already present.
  - A worker that emits a valid block in response to the second nudge completes
    the round successfully.
  - Three consecutive failures still fail the round, as today.

### U17. model-config-and-plumbing — make the model a settable value everywhere it is used

- **Goal**: three independently settable models — workers (coder and reviewer),
  the orchestrator's own base session, and the speccer/grouper — exist as config
  fields and are threaded to every place an argv is built. Defaults change to
  **Sonnet for workers, Opus for the orchestrator and the speccer**. The grouper's
  argv in `orchestrator/grouping/llm.py`, which today carries no model flag at
  all, gains one.
- **Files**: `orchestrator/config.py`,
  `orchestrator/execution/sessions.py`,
  `orchestrator/grouping/llm.py`,
  `tests/test_model_selection.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `model-config`
- **Verification**:
  - With no configuration, a forked worker's argv contains `--model` followed by
    a Sonnet model id.
  - With no configuration, the base session's argv contains `--model` followed by
    an Opus model id.
  - With no configuration, the grouper's argv contains `--model` followed by an
    Opus model id.
  - Setting the worker model in config leaves the speccer and base-session models
    at their own defaults.
  - Each of the three settings is readable back from the resolved config.

### U36. model-cli-flags — expose the three model settings on the command line

- **Goal**: `--model-worker`, `--model-base` and `--model-speccer` on the CLI,
  overriding the config file, which overrides the defaults. The run banner prints
  all three resolved model ids so an operator never has to infer from a
  transcript which model actually ran.
- **Files**: `orchestrator/cli.py`
- **Symbols**: —
- **Depends-on**: u17-model-config-and-plumbing
- **Slice**: —
- **Implements / Consumes**: implements `model-flags`; consumes `model-config`
- **Verification**:
  - `run --help` lists the three model flags.
  - A model flag on the command line overrides the same setting in the config
    file.
  - Omitting all three flags leaves the config-resolved values in effect.
  - The run banner prints the three resolved model ids.

### U18. resolved-options-on-form — show what the run will actually do, before it does it

- **Goal**: two things the launch form leaves the operator to discover
  afterwards. (R5) the three model fields are exposed and sent through
  `ExecutionOptions.to_argv`. (F14) the form shows the **resolved** value of every
  execution option that is left unspecified — most importantly `concurrency`,
  whose library default of 1 ran thirteen groups serially on a DAG whose widest
  wave is three, with nothing on the form suggesting that is what "leave it
  unspecified" means. The run header shows the same resolved values for a running
  run.
- **Files**: `orchestrator/observatory/launch.py`,
  `ui/src/components/launch/ExecutionOptions.tsx`,
  `ui/src/routes/Launch.tsx`,
  `ui/src/routes/launch.test.tsx`
- **Symbols**: —
- **Depends-on**: u17-model-config-and-plumbing, u36-model-cli-flags
- **Slice**: —
- **Implements / Consumes**: consumes `model-config`, `model-flags`
- **Verification**:
  - The launch form renders three model inputs, defaulted to the values the CLI
    would resolve.
  - An execution option left unspecified displays its resolved default next to
    the field, and `concurrency` displays `1` rather than an empty input.
  - Submitting the form with a worker model set produces an argv containing that
    model's flag.
  - Submitting with everything left at its default produces an argv that still
    runs.
  - The run header for a running run displays its resolved concurrency and its
    three model ids.

### U19. resume-refreshes-record — stop a resumed run describing its original launch

- **Goal**: a resume rewrites the run's on-disk record instead of inheriting a
  stale one. The `RunManifest` write moves out of the base-session branch (or
  gains a resume-side update) so the effective config — `escalation`,
  `usage_limit`, and now the resolved models — reflects the process actually
  running (F20). And a resume stamps `released_at` on any armed
  `usage-limit.json` before it starts work, since by definition the pause that
  record describes is over, so a live run stops rendering as paused (F21).
- **Files**: `orchestrator/cli.py`,
  `orchestrator/execution/ratelimit.py`,
  `tests/test_resume_record.py` *(new, medium)*
- **Symbols**: —
  `_release_locked`, `to_dict`
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `resume-record`
- **Verification**:
  - Resuming a run launched without HITL, using a flag that enables it, produces
    a snapshot whose `escalation.enabled` is true.
  - The snapshot's escalation settings after a resume match what the resumed
    process's own `run.log` line reports.
  - Resuming a run whose `usage-limit.json` is armed with a past `wake_at` and a
    null `released_at` leaves `released_at` set once work starts.
  - The Observatory's usage-limit banner does not render for that resumed run
    while groups are working.
  - A resume that itself hits a fresh usage limit arms a new record and the
    banner renders again.

### U20. resume-form-prefill — a resume should not silently differ from the run it continues

- **Goal**: the resume card selects the run being viewed and pre-fills the
  execution options that run last used, drawn from the job record's `options`
  block (`.orchestrator/jobs/<id>/command.json`) and the manifest, so the operator
  changes only what they mean to change. Because U19 makes the manifest record the
  *last-used* config rather than the launch config, the pre-fill is trustworthy.
- **Files**: `orchestrator/observatory/launch.py`,
  `ui/src/routes/Launch.tsx`,
  `ui/src/components/launch/ExecutionOptions.tsx`,
  `ui/src/routes/launch.test.tsx`
- **Symbols**: —
  `list_jobs`, `load_manifest`
- **Depends-on**: u19-resume-refreshes-record
- **Slice**: —
- **Implements / Consumes**: consumes `resume-record`
- **Verification**:
  - Opening the resume card while viewing a run pre-selects that run.
  - The card's execution fields are populated from the run's last-used options,
    not left blank.
  - A run whose job record is missing renders the card with defaults and a note
    saying the previous options could not be recovered, rather than silently
    blank.
  - Submitting the pre-filled card without edits produces an argv equivalent to
    the run's last-used options.

### U21. group-form-options — let the group form express what the endpoint already accepts

- **Goal**: `GroupCard` offers `granularity`, `token_budget` and `auto_resume`
  alongside plan, name and dry-run — `POST /jobs/group` already accepts all
  three, so trying `--granularity balanced` no longer sends the operator back to
  the terminal.
- **Files**: `ui/src/routes/Launch.tsx`,
  `ui/src/routes/Launch.css`,
  `ui/src/routes/launch.test.tsx`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: jobs
- **Implements / Consumes**: consumes `job-api`
- **Verification**:
  - The group form renders inputs for granularity, token budget and auto-resume.
  - Submitting with granularity set to `balanced` posts a body whose
    `granularity` is `balanced`.
  - Submitting with all three left unset posts a body omitting them, and the job
    still starts.
  - The resolved values are visible on the submitted job's record.

### U22. launch-live-refresh — a finished grouping should become visible without a reload

- **Goal**: `Launch.tsx` refetches plans, groupings and runs while the page is
  open, so a grouping that completes becomes selectable in "Start a run" without
  a reload (F3); and `JobLog` refetches the job so `running` reflects reality
  instead of being frozen at POST time (F6).
- **Files**: `ui/src/routes/Launch.tsx`,
  `ui/src/components/launch/JobLog.tsx`,
  `ui/src/routes/launch.test.tsx`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: jobs
- **Implements / Consumes**: consumes `job-api`
- **Verification**:
  - A grouping that appears server-side while the launch page is open becomes
    selectable in the run form without a reload.
  - A job that finishes while its log is open stops reporting "running" without a
    reload.
  - The page stops refetching when it is unmounted.
  - A failed refetch leaves the last known data on screen rather than blanking
    the form.

### U23. job-routes — make a launched job addressable

- **Goal**: `/p/:project/jobs` and `/p/:project/jobs/:id` routes, backed by the
  existing `GET /api/projects/{p}/jobs`, so a refresh, a navigation or a second
  tab does not lose sight of a running job. A job no longer lives only in
  `Launch` component state.
- **Files**: `ui/src/routes.tsx`,
  `ui/src/routes.test.tsx`,
  `ui/src/routes/Jobs.tsx` *(new, medium)*,
  `ui/src/routes/JobDetail.tsx` *(new, medium)*,
  `ui/src/routes/Launch.tsx`
- **Symbols**: —
- **Depends-on**: u22-launch-live-refresh
- **Slice**: jobs
- **Implements / Consumes**: implements `job-routes`; consumes `job-api`
- **Verification**:
  - Navigating to `/p/<project>/jobs` lists the project's jobs with their kind,
    status and start time.
  - Navigating to `/p/<project>/jobs/<id>` renders that job's log, streaming if
    it is still running.
  - Reloading the job detail page for a running job resumes streaming its log.
  - Launching a job navigates to, or links to, its detail route.
  - A job id that does not exist renders a not-found state rather than an error.

### U24. group-stage-progress — stop a grouping job showing nothing for three and a half minutes

- **Goal**: the `group` CLI emits unbuffered stage progress — mapper → graph →
  partition → specs *i*/*N* — so there is something to stream, and the UI renders
  a determinate progress bar driven by those lines plus an elapsed timer. `spec
  i/N` is the only long stage and it is countable, so a real percentage is
  available rather than a spinner.
- **Files**: `orchestrator/cli.py`,
  `orchestrator/grouping/pipeline.py`,
  `ui/src/components/launch/JobLog.tsx`,
  `ui/src/components/launch/JobProgress.tsx` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `job-progress`
- **Verification**:
  - A `group` invocation writes its first progress line to the job log within
    five seconds of starting.
  - The job log contains a line per stage, and one countable line per spec in the
    form `spec i/N`.
  - The progress output is unbuffered: the lines appear in the log file while the
    command is still running, not only at exit.
  - The UI renders a determinate bar whose percentage advances as `spec i/N`
    lines arrive, and an elapsed timer.
  - A job producing no recognisable progress lines falls back to the current
    indeterminate display rather than a stuck bar.

### U25. grouping-preview-on-launch — read what is about to be launched, before launching it

- **Goal**: when a grouping already exists, the launch page shows its groups —
  names, tasks, files, estimates, dependencies — the same listing a dry run
  prints, so what is about to be launched can be read without a terminal and
  without a throwaway dry run. Today the grouping dropdown shows only a name and
  a count.
- **Files**: `orchestrator/observatory/grouping.py`,
  `ui/src/routes/Launch.tsx`,
  `ui/src/components/launch/GroupingPreview.tsx` *(new, medium)*,
  `ui/src/routes/launch.test.tsx`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: consumes `job-api`
- **Verification**:
  - Selecting an existing grouping on the launch page renders its groups with
    names, task lists, file lists, token estimates and dependencies.
  - The rendered group list matches what `group --dry-run` prints for the same
    grouping.
  - Selecting a grouping whose `groups.json` is absent renders an explanatory
    empty state rather than an error.
  - The preview is read-only and launching is still an explicit action.

### U26. heartbeat-pause-fields — say that fifty-seven of those fifty-eight minutes were a pause

- **Goal**: `GroupHeartbeat` passes through `paused_s` and `round_elapsed_s`,
  which are already on disk, and the group card shows the paused portion of a
  long phase. A card reading "forking the base session — 58m" gains the fact that
  57 of those minutes were a deliberate usage-limit pause.
- **Files**: `orchestrator/observatory/runs.py`,
  `ui/src/components/GroupBoard.tsx`,
  `ui/src/components/GroupBoard.test.tsx`
- **Symbols**: —
  `build_snapshot`
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `heartbeat-pause-fields`
- **Verification**:
  - A heartbeat file carrying `paused_s` and `round_elapsed_s` produces a
    snapshot group exposing both.
  - A heartbeat file lacking either field produces a snapshot group with that
    field absent, and the card renders without it.
  - A group card whose heartbeat records `phase_elapsed_s: 3529` and
    `paused_s: 3472` shows the paused portion distinctly from the phase elapsed.
  - The stall inference is unchanged: a group paused with an advancing
    `updated_at` is still not reported as stalled.

### U27. run-banner-and-log-timezone — stop the run's own output misleading its reader

- **Goal**: (F11) the run banner stops naming a config file that does not exist —
  it says "defaults (no config file)" when none was read, and names the path only
  when one was. (F22) the run log is stamped in the operator's local zone, or both
  the line prefix and any quoted reset instant are rendered in one zone and
  labelled once at the top of the file. The provider's own wording ("resets Aug
  25, 1pm") is still passed through verbatim as evidence, never paraphrased or
  re-zoned.
- **Files**: `orchestrator/cli.py`, `orchestrator/execution/ratelimit.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `local-zone-logging`
- **Verification**:
  - Starting a run with no `.orchestrator/config.toml` prints a banner saying the
    values are defaults and naming no path.
  - Starting a run with a config file present prints its path.
  - A run log line reporting a pause shows its prefix and the reset instant in the
    same zone.
  - The log states its zone once, at the top of the file.
  - The provider's verbatim wording still appears unaltered in the log line.

### U35. generation-naming-and-ui-timestamps — stop `-g3` reading as "group 3"

- **Goal**: (F17) the Observatory renders a session's generation as `gen 3`, so
  `r20260820-213134-g1-coder-g3` stops reading as though it involves group g3.
  (F22, UI half) wherever the Observatory prints a raw timestamp it uses the
  operator's local zone and labels it, matching what the run log now does.
- **Files**: `ui/src/components/AttemptGrid.tsx`,
  `ui/src/components/GroupDrillIn.tsx`,
  `ui/src/components/AttemptGrid.test.tsx`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: legibility
- **Implements / Consumes**: consumes `local-zone-logging`
- **Verification**:
  - A session id ending `-coder-g3` renders in the attempt grid labelled `gen 3`.
  - That label is visually distinguishable from a group named g3 on the same page.
  - A timestamp rendered in the drill-in shows the operator's local zone and names
    the zone.
  - A session with no generation suffix renders without a generation label rather
    than as `gen 0`.

### U28. drill-in-intensity-difficulty — explain why one group in thirteen got a second opinion

- **Goal**: the group drill-in shows the group's `intensity` and `difficulty`
  score next to it, and labels a `-extra` verdict as the mandatory second
  verification pass that a `paired_plus` group earns by scoring above `d_hard`.
  An operator meeting `verdict-g1-r1-extra.json` learns why this group and no
  other has one. The known `_VERDICT_RE` ambiguity is documented rather than
  changed.
- **Files**: `orchestrator/observatory/runs.py`,
  `orchestrator/observatory/artifacts.py`,
  `ui/src/components/GroupDrillIn.tsx`,
  `ui/src/components/GroupDrillIn.css`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: legibility
- **Implements / Consumes**: consumes `heartbeat-pause-fields`
- **Verification**:
  - The drill-in for a group shows its difficulty score and its intensity tier.
  - A group whose difficulty exceeds `d_hard` shows `paired_plus` and its
    `-extra` verdict is labelled as the mandatory second pass.
  - A group with no `-extra` verdict renders no extra-pass label.
  - The extra verdict opens in the same viewer as the first-pass verdict, with
    both distinguishable by label.

### U29. generation-and-group-diffs — see what a generation and a group actually contributed

- **Goal**: two diff views. Per generation (R3): when a generation ends, its
  final diff rendered the way git would — the only honest way to see whether
  generations 3 and 4 re-derived work generations 1 and 2 had already committed.
  Per group (R4): once a group completes, its whole diff against the integration
  tip it branched from, which is a single `git diff <tip>..<group branch>` call
  and the view an operator wants most often.
- **Files**: `orchestrator/observatory/artifacts.py`,
  `orchestrator/observatory/runs.py`,
  `ui/src/components/DiffView.tsx` *(new, large)*,
  `ui/src/components/GroupDrillIn.tsx`
- **Symbols**: —
  `build_snapshot`
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `diff-api`
- **Verification**:
  - Requesting a completed group's diff returns the diff between the integration
    tip it branched from and its group branch.
  - Requesting a generation's diff returns the final state of what that
    generation changed, not a running feed.
  - A group whose branch has been torn down returns an explanatory empty state
    rather than an error.
  - The drill-in renders both diffs with per-file headers and is scrollable
    without the page scrolling horizontally.
  - A diff larger than a stated size threshold is truncated with the truncation
    stated, rather than sent whole.

### U30. orchestrator-session-on-board — put the orchestrator's own work where its consequences are

- **Goal**: the orchestrator's own sessions — the base session it establishes,
  the speccer, and the rewrite calls it makes on a group's behalf — appear on the
  group card and in the attempt-history grid alongside the sessions they drive,
  so a generation that exists because the *orchestrator* rewrote the spec is
  legible as such.
- **Files**: `orchestrator/observatory/runs.py`,
  `ui/src/components/AttemptGrid.tsx`,
  `ui/src/components/GroupBoard.tsx`,
  `ui/src/components/AttemptGrid.test.tsx`
- **Symbols**: —
  `SessionEntry`, `session_display_name`
- **Depends-on**: u14-rewrite-observability
- **Slice**: —
- **Implements / Consumes**: consumes `informational-surprise`
- **Verification**:
  - A run's snapshot exposes the base session as a session with an orchestrator
    role.
  - A group that was re-specced shows the orchestrator's rewrite call in its
    attempt history, positioned before the generation it produced.
  - The attempt grid visually distinguishes orchestrator sessions from coder and
    reviewer sessions.
  - A group never re-specced shows no orchestrator rows beyond the base session.

### U31. speccer-runs-on-grouping-tab — show the LLM half of a grouping

- **Goal**: the grouping tab lists the speccer's LLM runs — already persisted in
  the grouping directory's `llm/` records and snapshotted into the run — as
  sessions, openable in the same viewer used for coder and reviewer sessions.
  Today the tab renders only the algorithmic partition.
- **Files**: `orchestrator/observatory/grouping.py`,
  `orchestrator/observatory/transcripts.py`,
  `ui/src/components/grouping/GroupingTab.tsx`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: consumes `diff-api`
- **Verification**:
  - The grouping tab lists one row per recorded speccer call, with its model and
    token usage.
  - Opening a speccer call shows its prompt and response in the session viewer.
  - A grouping whose `llm/` directory is absent renders the partition with an
    explanatory empty state for the LLM section.
  - Rewrite speccer calls recorded during the run appear alongside the
    grouping-time calls, distinguishable by when they ran.

### U32. integration-worktree-provisioning — make the tree that represents the run's output runnable

- **Goal**: `IntegrationMerger.ensure()` provisions the integration worktree with
  the same `provision_args` group worktrees get, so an operator going there to run
  what was just built finds an environment. And every worktree's provisioning is
  logged with the exact `uv sync` invocation used, with the worktree path and its
  provisioning state surfaced in the group drill-in — "this worktree was
  provisioned with `uv sync --all-extras` at 21:34" is one line and ends the
  confusion.
- **Files**: `orchestrator/execution/merge.py`,
  `orchestrator/cli.py`,
  `orchestrator/observatory/runs.py`,
  `ui/src/components/GroupDrillIn.tsx`,
  `tests/test_integration_provisioning.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - After a run establishes the integration worktree, that worktree contains a
    provisioned environment.
  - The run log contains one line per provisioned worktree naming the worktree
    path and the exact provisioning command.
  - The group drill-in shows the group's worktree path and its provisioning
    state and time.
  - A provisioning failure is logged and reported rather than leaving the
    worktree silently unprovisioned.
  - A group whose worktree was torn down still shows its recorded provisioning
    line rather than a blank.

### U33. observatory-packaging — declare what the Observatory imports

- **Goal**: `fastapi` and `uvicorn` are declared in `pyproject.toml` rather than
  arriving transitively via fastmcp, so `smart-mcps-orchestrate ui` works in a
  freshly installed tool environment instead of failing with "the Observatory
  needs fastapi and uvicorn installed" until a `uv tool install --force`.
- **Files**: `pyproject.toml`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `fastapi` and `uvicorn` appear in the project's dependency table.
  - `uv sync` in a clean environment installs both.
  - `smart-mcps-orchestrate ui --help` succeeds in an environment built only from
    the lock, with no manual installation.
  - The lock file resolves without conflict after the declaration.

### U34. docs-refresh — bring the reference documentation up to the behaviour that now exists

- **Goal**: `docs/orchestrator-reference.html` and the base docs are updated for
  the structural changes this plan makes: preflight failure classification and
  the baseline, the auth ladder, the content-based index fingerprint and its
  index-stability-not-reproducibility caveat, the informational surprise kind and
  the rewrite budget, worker ground rules living in the shared context, the three
  model knobs and their new defaults, and integration-worktree provisioning. The
  per-TTL cache-creation finding from U9 is recorded here.
- **Files**: `docs/orchestrator-reference.html`,
  `docs/observatory.md`,
  `docs/orchestrator-grouping.md`,
  `docs/orchestrator-grouping-config.md`,
  `CONTEXT.md`
- **Symbols**: —
- **Depends-on**: u3-merge-gate-triage, u4-auth-refresh-ladder,
  u7-fingerprint-compare-on-resume, u9-spend-vs-occupancy,
  u13-informational-surprise-kind, u15-hoist-ground-rules,
  u17-model-config-and-plumbing, u36-model-cli-flags,
  u32-integration-worktree-provisioning
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - The reference HTML documents the three preflight failure kinds and what each
    routes to.
  - The reference HTML documents the auth ladder's three rungs.
  - The grouping docs state that the fingerprint is a content hash, that the
    quiescence handshake runs before partitioning, and that grouping is
    index-stable but not reproducible while the mapper is an unseeded LLM.
  - The docs state that an informational surprise does not consume a rewrite.
  - The docs state the new default models for each of the three roles.
  - The docs state that both group and integration worktrees are provisioned, and
    with what.
  - `CONTEXT.md`'s glossary carries entries for the terms this plan introduces
    (preflight kind, baseline, informational surprise, auth pause).
  - No document still describes a behaviour this plan changed.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-preflight-classification
    description: Classify a preflight failure as env, timeout or regression before raising
    slice: null
    files:
      - orchestrator/execution/preflight.py
      - tests/test_preflight.py
    symbols: []
    depends_on: []
    implements: ["preflight-failure-kind"]
    consumes: []

  - task_id: u2-preflight-baseline
    description: Record the check command's failing-test set on the launch branch and diff new failures against it
    slice: null
    files:
      - orchestrator/execution/preflight.py
      - orchestrator/cli.py
      - tests/test_preflight_baseline.py
    size_hints:
      tests/test_preflight_baseline.py: medium
    symbols: []
    depends_on: [u1-preflight-classification]
    implements: ["preflight-baseline"]
    consumes: ["preflight-failure-kind"]

  - task_id: u3-merge-gate-triage
    description: Route a merge-gate failure by its classified cause and hand the next generation a real diagnosis
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/execution/merge.py
      - tests/test_merge_gate_triage.py
    size_hints:
      tests/test_merge_gate_triage.py: large
    symbols: []
    depends_on: [u1-preflight-classification, u2-preflight-baseline]
    implements: []
    consumes: ["preflight-failure-kind", "preflight-baseline"]

  - task_id: u4-auth-refresh-ladder
    description: Check token expiry after a long pause, refresh it from the unconfined orchestrator, and pause instead of halting
    slice: null
    files:
      - orchestrator/execution/auth.py
      - orchestrator/execution/sessions.py
      - orchestrator/execution/ratelimit.py
      - tests/test_auth_ladder.py
    size_hints:
      orchestrator/execution/auth.py: medium
      tests/test_auth_ladder.py: medium
    symbols: []
    depends_on: []
    implements: ["auth-pause"]
    consumes: []

  - task_id: u5-index-fingerprint-content
    description: Compute the index fingerprint from a canonical logical export instead of operational counters
    slice: repro
    files:
      - orchestrator/grouping/graphing.py
      - orchestrator/grouping/trace.py
      - tests/test_index_fingerprint.py
    size_hints:
      tests/test_index_fingerprint.py: medium
    symbols: []
    depends_on: []
    implements: ["index-fingerprint-v2"]
    consumes: []

  - task_id: u6-index-quiescence
    description: Poll the index until its fingerprint is stable before partitioning, failing loudly on a timeout
    slice: repro
    files:
      - orchestrator/grouping/graphing.py
      - orchestrator/grouping/pipeline.py
      - tests/test_index_quiescence.py
    size_hints:
      tests/test_index_quiescence.py: medium
    symbols: []
    depends_on: [u5-index-fingerprint-content]
    implements: []
    consumes: ["index-fingerprint-v2"]

  - task_id: u7-fingerprint-compare-on-resume
    description: Compare the recorded fingerprint on the resume path and refuse to reuse a partition built against a different index
    slice: null
    files:
      - orchestrator/grouping/pipeline.py
      - orchestrator/cli.py
      - tests/test_fingerprint_compare.py
    size_hints:
      tests/test_fingerprint_compare.py: medium
    symbols: []
    depends_on: [u5-index-fingerprint-content]
    implements: []
    consumes: ["index-fingerprint-v2"]

  - task_id: u8-specless-preview-dir
    description: Write specless and dry-run traces to a preview location instead of beside a different partition's groups.json
    slice: null
    files:
      - orchestrator/cli.py
      - orchestrator/grouping/pipeline.py
      - tests/test_specless_preview.py
    size_hints:
      tests/test_specless_preview.py: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []

  - task_id: u9-spend-vs-occupancy
    description: Report spend from the envelope's all-turns usage while occupancy keeps reading the last turn
    slice: accounting
    files:
      - orchestrator/execution/sessions.py
      - orchestrator/model.py
      - ui/src/components/CostPanel.tsx
      - tests/test_session_spend.py
    size_hints:
      tests/test_session_spend.py: medium
    symbols: []
    depends_on: []
    implements: ["session-spend"]
    consumes: []

  - task_id: u10-calibration-generations
    description: Label multi-generation calibration rows, report their peak, and exclude them from the summary
    slice: accounting
    files:
      - ui/src/components/CostPanel.tsx
      - ui/src/components/CostPanel.test.tsx
      - orchestrator/observatory/runs.py
    symbols: []
    depends_on: [u9-spend-vs-occupancy]
    implements: []
    consumes: ["session-spend"]

  - task_id: u11-surprise-validation
    description: Validate surprise recipient ids at mark time, resolve task ids to groups, warn on wide fan-out and dedupe
    slice: surprises
    files:
      - orchestrator/execution/review.py
      - tests/test_surprise_board.py
    size_hints:
      tests/test_surprise_board.py: medium
    symbols: []
    depends_on: []
    implements: ["surprise-validation"]
    consumes: []

  - task_id: u12-surprise-residue-report
    description: Report undelivered surprises by bucket with a reason, and surface the pending board in the Observatory
    slice: null
    files:
      - orchestrator/execution/finish.py
      - orchestrator/execution/review.py
      - orchestrator/observatory/runs.py
      - ui/src/components/SurpriseBoard.tsx
      - ui/src/components/GroupDrillIn.tsx
    size_hints:
      ui/src/components/SurpriseBoard.tsx: medium
    symbols: []
    depends_on: [u11-surprise-validation]
    implements: []
    consumes: ["surprise-validation"]

  - task_id: u13-informational-surprise-kind
    description: Add an informational surprise kind that briefs the next generation without consuming a rewrite
    slice: surprises
    files:
      - orchestrator/model.py
      - orchestrator/execution/review.py
      - tests/test_informational_surprise.py
    size_hints:
      tests/test_informational_surprise.py: medium
    symbols: []
    depends_on: []
    implements: ["informational-surprise"]
    consumes: []

  - task_id: u14-rewrite-observability
    description: Log every spec rewrite, persist the rewritten spec, and record the rewrite speccer call
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/cli.py
      - tests/test_rewrite_observability.py
    size_hints:
      tests/test_rewrite_observability.py: medium
    symbols: []
    depends_on: [u13-informational-surprise-kind]
    implements: []
    consumes: ["informational-surprise"]

  - task_id: u15-hoist-ground-rules
    description: Move the worker ground rules into the compiled base context and keep a compact report-block tail per fork
    slice: prompting
    files:
      - orchestrator/grouping/base_context.py
      - orchestrator/execution/prompting.py
      - orchestrator/prompts/coder.md
      - orchestrator/prompts/reviewer.md
      - orchestrator/prompts/report_contract.md
      - orchestrator/prompts/worker_ground_rules.md
    size_hints:
      orchestrator/prompts/worker_ground_rules.md: medium
    symbols: []
    depends_on: []
    implements: ["worker-ground-rules"]
    consumes: []

  - task_id: u16-smart-nudges
    description: Give the first nudge the verbatim contract and the second a filled-in skeleton to transcribe
    slice: prompting
    files:
      - orchestrator/execution/sessions.py
      - orchestrator/execution/prompting.py
      - tests/test_nudges.py
    size_hints:
      tests/test_nudges.py: medium
    symbols: []
    depends_on: [u15-hoist-ground-rules]
    implements: []
    consumes: ["worker-ground-rules"]

  - task_id: u17-model-config-and-plumbing
    description: Add worker, orchestrator and speccer model settings with Sonnet workers and Opus elsewhere by default
    slice: null
    files:
      - orchestrator/config.py
      - orchestrator/execution/sessions.py
      - orchestrator/grouping/llm.py
      - tests/test_model_selection.py
    size_hints:
      tests/test_model_selection.py: medium
    symbols: []
    depends_on: []
    implements: ["model-config"]
    consumes: []

  - task_id: u36-model-cli-flags
    description: Add the three model CLI flags and print the resolved model ids in the run banner
    slice: null
    files:
      - orchestrator/cli.py
    symbols: []
    depends_on: [u17-model-config-and-plumbing]
    implements: ["model-flags"]
    consumes: ["model-config"]

  - task_id: u18-resolved-options-on-form
    description: Expose the three model knobs and show every execution option's resolved default on the launch form
    slice: null
    files:
      - orchestrator/observatory/launch.py
      - ui/src/components/launch/ExecutionOptions.tsx
      - ui/src/routes/Launch.tsx
      - ui/src/routes/launch.test.tsx
    symbols: []
    depends_on: [u17-model-config-and-plumbing, u36-model-cli-flags]
    implements: []
    consumes: ["model-config", "model-flags"]

  - task_id: u19-resume-refreshes-record
    description: Persist the effective config on resume and release a usage-limit record the resumed process inherited
    slice: null
    files:
      - orchestrator/cli.py
      - orchestrator/execution/ratelimit.py
      - tests/test_resume_record.py
    size_hints:
      tests/test_resume_record.py: medium
    symbols: []
    depends_on: []
    implements: ["resume-record"]
    consumes: []

  - task_id: u20-resume-form-prefill
    description: Pre-select the viewed run on the resume card and pre-fill the options it last used
    slice: null
    files:
      - orchestrator/observatory/launch.py
      - ui/src/routes/Launch.tsx
      - ui/src/components/launch/ExecutionOptions.tsx
      - ui/src/routes/launch.test.tsx
    symbols: []
    depends_on: [u19-resume-refreshes-record]
    implements: []
    consumes: ["resume-record"]

  - task_id: u21-group-form-options
    description: Offer granularity, token budget and auto-resume on the group form
    slice: jobs
    files:
      - ui/src/routes/Launch.tsx
      - ui/src/routes/Launch.css
      - ui/src/routes/launch.test.tsx
    symbols: []
    depends_on: []
    implements: []
    consumes: ["job-api"]

  - task_id: u22-launch-live-refresh
    description: Refetch plans, groupings, runs and job status while the launch page is open
    slice: jobs
    files:
      - ui/src/routes/Launch.tsx
      - ui/src/components/launch/JobLog.tsx
      - ui/src/routes/launch.test.tsx
    symbols: []
    depends_on: []
    implements: []
    consumes: ["job-api"]

  - task_id: u23-job-routes
    description: Add addressable jobs and job-detail routes backed by the existing jobs endpoint
    slice: jobs
    files:
      - ui/src/routes.tsx
      - ui/src/routes.test.tsx
      - ui/src/routes/Jobs.tsx
      - ui/src/routes/JobDetail.tsx
      - ui/src/routes/Launch.tsx
    size_hints:
      ui/src/routes/Jobs.tsx: medium
      ui/src/routes/JobDetail.tsx: medium
    symbols: []
    depends_on: [u22-launch-live-refresh]
    implements: ["job-routes"]
    consumes: ["job-api"]

  - task_id: u24-group-stage-progress
    description: Emit unbuffered stage progress from the group CLI and render a determinate progress bar
    slice: null
    files:
      - orchestrator/cli.py
      - orchestrator/grouping/pipeline.py
      - ui/src/components/launch/JobLog.tsx
      - ui/src/components/launch/JobProgress.tsx
    size_hints:
      ui/src/components/launch/JobProgress.tsx: medium
    symbols: []
    depends_on: []
    implements: ["job-progress"]
    consumes: []

  - task_id: u25-grouping-preview-on-launch
    description: Show an existing grouping's groups, tasks, files, estimates and dependencies on the launch page
    slice: null
    files:
      - orchestrator/observatory/grouping.py
      - ui/src/routes/Launch.tsx
      - ui/src/components/launch/GroupingPreview.tsx
      - ui/src/routes/launch.test.tsx
    size_hints:
      ui/src/components/launch/GroupingPreview.tsx: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: ["job-api"]

  - task_id: u26-heartbeat-pause-fields
    description: Pass paused_s and round_elapsed_s through the snapshot and show the paused portion on the group card
    slice: null
    files:
      - orchestrator/observatory/runs.py
      - ui/src/components/GroupBoard.tsx
      - ui/src/components/GroupBoard.test.tsx
    symbols: []
    depends_on: []
    implements: ["heartbeat-pause-fields"]
    consumes: []

  - task_id: u27-run-banner-and-log-timezone
    description: Report defaults when no config file was read and stamp the run log in one labelled zone
    slice: null
    files:
      - orchestrator/cli.py
      - orchestrator/execution/ratelimit.py
    symbols: []
    depends_on: []
    implements: ["local-zone-logging"]
    consumes: []

  - task_id: u35-generation-naming-and-ui-timestamps
    description: Render a session's generation as gen N and print Observatory timestamps in the operator's labelled zone
    slice: legibility
    files:
      - ui/src/components/AttemptGrid.tsx
      - ui/src/components/GroupDrillIn.tsx
      - ui/src/components/AttemptGrid.test.tsx
    symbols: []
    depends_on: []
    implements: []
    consumes: ["local-zone-logging"]

  - task_id: u28-drill-in-intensity-difficulty
    description: Show a group's intensity and difficulty in the drill-in and label the extra verdict as the mandatory second pass
    slice: legibility
    files:
      - orchestrator/observatory/runs.py
      - orchestrator/observatory/artifacts.py
      - ui/src/components/GroupDrillIn.tsx
      - ui/src/components/GroupDrillIn.css
    symbols: []
    depends_on: []
    implements: []
    consumes: ["heartbeat-pause-fields"]

  - task_id: u29-generation-and-group-diffs
    description: Render each generation's finished diff and each completed group's diff against the tip it branched from
    slice: null
    files:
      - orchestrator/observatory/artifacts.py
      - orchestrator/observatory/runs.py
      - ui/src/components/DiffView.tsx
      - ui/src/components/GroupDrillIn.tsx
    size_hints:
      ui/src/components/DiffView.tsx: large
    symbols: []
    depends_on: []
    implements: ["diff-api"]
    consumes: []

  - task_id: u30-orchestrator-session-on-board
    description: Put the orchestrator's base, speccer and rewrite sessions on the group card and in the attempt history
    slice: null
    files:
      - orchestrator/observatory/runs.py
      - ui/src/components/AttemptGrid.tsx
      - ui/src/components/GroupBoard.tsx
      - ui/src/components/AttemptGrid.test.tsx
    symbols: []
    depends_on: [u14-rewrite-observability]
    implements: []
    consumes: ["informational-surprise"]

  - task_id: u31-speccer-runs-on-grouping-tab
    description: List the speccer's recorded LLM runs on the grouping tab and open them in the session viewer
    slice: null
    files:
      - orchestrator/observatory/grouping.py
      - orchestrator/observatory/transcripts.py
      - ui/src/components/grouping/GroupingTab.tsx
    symbols: []
    depends_on: []
    implements: []
    consumes: ["diff-api"]

  - task_id: u32-integration-worktree-provisioning
    description: Provision the integration worktree like a group worktree and log every worktree's provisioning
    slice: null
    files:
      - orchestrator/execution/merge.py
      - orchestrator/cli.py
      - orchestrator/observatory/runs.py
      - ui/src/components/GroupDrillIn.tsx
      - tests/test_integration_provisioning.py
    size_hints:
      tests/test_integration_provisioning.py: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []

  - task_id: u33-observatory-packaging
    description: Declare fastapi and uvicorn as project dependencies instead of relying on a transitive install
    slice: null
    files:
      - pyproject.toml
    symbols: []
    depends_on: []
    implements: []
    consumes: []

  - task_id: u34-docs-refresh
    description: Update the reference HTML and base docs for the structural behaviour changes this plan makes
    slice: null
    files:
      - docs/orchestrator-reference.html
      - docs/observatory.md
      - docs/orchestrator-grouping.md
      - docs/orchestrator-grouping-config.md
      - CONTEXT.md
    symbols: []
    depends_on:
      - u3-merge-gate-triage
      - u4-auth-refresh-ladder
      - u7-fingerprint-compare-on-resume
      - u9-spend-vs-occupancy
      - u13-informational-surprise-kind
      - u15-hoist-ground-rules
      - u17-model-config-and-plumbing
      - u36-model-cli-flags
      - u32-integration-worktree-provisioning
    implements: []
    consumes: []
```
