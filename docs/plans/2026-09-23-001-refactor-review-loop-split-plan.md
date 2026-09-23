---
title: Split the review loop into responsibility modules with zero behaviour change
type: refactor
date: 2026-09-23
origin: docs/brainstorms/2026-09-21-unit-recipes-requirements.md (Plan A of its Next Step; direct description for the refactor itself)
---

# Split the review loop into responsibility modules with zero behaviour change

## Objective

`orchestrator/execution/review.py` (1,993 lines, 97 KB) is one class,
`_GroupExecution`, that owns every responsibility a group's execution has:
generation lifecycle, the coder↔reviewer round, session records and usage,
the merge ladder, escalation and rewrite, surprises and prompt notes. The Unit
Recipes brainstorm (R9 `shared-plumbing`) needs those responsibilities as
separately importable pieces so a second executor can compose the ones it
needs without inheriting the coder loop. This plan does exactly the split and
nothing else: **six modules, one composition root, every method body moved
byte-for-byte, and the observable behaviour of a run identical.** It is the
"measurable beginning" the brainstorm names — the measure is the existing
suite (1,935 tests on this branch, 104 s) passing with no test *body* changed,
plus a layout test and a live-tier pass proving the new modules are real
homes and the heartbeat still beats.

Not in scope: `orchestrator/cli.py` (3,258 lines) and `sessions.py` are left
alone; no recipe, registry or dispatcher; no rename of `review.py` (that is
Plan B's decision, when it becomes the `code` recipe's executor).

## What we already know (resolved context)

**The file and its public surface.** `orchestrator/execution/review.py`
exports, and is imported for, exactly these names (verified 2026-09-23 by
grep over `orchestrator/` and `tests/`):

| name                                                                     | defined at                                                                   | imported by                                                                                                                                                                          |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `MergeConflict`                                                          | review.py:119                                                                | `execution/merge.py:21` (raises it), `cli.py:97`, `tests/test_merge.py`, `tests/test_preflight.py`, `tests/test_review_loop.py`                                                      |
| `SurpriseBoard`, `surprise_residue`, `format_residue_report`, `REASON_*` | review.py:154–352                                                            | `execution/finish.py:29`, `observatory/runs.py:30`, `cli.py:97`, `tests/test_finish.py`, `tests/test_surprise_board.py`, `tests/test_review_scratch.py`, `tests/test_review_loop.py` |
| `ReviewDeps`, `make_executor`                                            | review.py:354, 397                                                           | `cli.py:97` (single construction site at `cli.py:2286`), `tests/test_review_loop.py`, `tests/test_review_scratch.py`, `tests/test_suspend_cure.py`                                   |
| `GroupFailure`                                                           | **not defined here** — re-exported by accident from `execution/scheduler.py` | `tests/test_scheduler.py`, `tests/test_review_recovery_log.py`, `tests/test_merge_gate_triage.py`, `tests/test_review_loop.py`                                                       |
| `_GroupExecution`                                                        | review.py:406                                                                | `tests/test_liveness_wiring.py`                                                                                                                                                      |
| `_spec_hash`                                                             | review.py:1963                                                               | `tests/test_review_loop.py`                                                                                                                                                          |

Eleven test files and four package modules import from it. `tests/test_review_loop.py`
(2,145 lines, 70 tests) is the main harness: it drives `make_executor` with a
`StubRunner` and a `Harness`, and `test_suspend_cure.py`, `test_liveness_wiring.py`
and `test_merge_gate_triage.py` import that harness.

**The class's coupling map.** A script over the method bodies (kept in the
plan's Decisions rationale, not in the repo) shows which instance state each
method touches. Shared state across responsibilities:

- `coder_entry`, `coder_sid`, `reviewer_sid`: written in the generation body
  and `_reenter`, read by records, reviewer, merge (`_resolve_conflict_in_place`)
  and escalation (`_resolve_needs_input`).
- `handoff_prompt`: set by `_prepare_handoff` (generation), `_rewrite` and
  `_relaunch` (escalation); consumed by the generation body.
- `sessions_spawned`, `rewrites`: bumped by `_record`, `_reenter`, `_rewrite`,
  `_relaunch`; read by `_breaker_reason`.
- `_flake_reruns`, `_untracked_strikes`: merge ladder only, reset per
  generation in the generation body.
- `_briefing_notes`, `_operator_notes`, `_env_failure`, `_grant_notes`: the
  prompt-note channels; written by surprises/escalation/retire, read by the
  `_apply_*` helpers when the next prompt is built.
- `_current_round_no`: written by `_round_tag`, read by `_on_content_filtered`.
- `_heartbeat`, `_cures`, `deps`, `ctx`, `group`, `gid`, `generation`,
  `workspace`: constructed in `__init__`, read everywhere.

Every one of these is set in `__init__` (review.py:409–478) or in the
generation body; no method creates a new attribute later. That is what makes
mixins over one `self` safe: the composition root's `__init__` is the single
place state is born.

**Module-level helpers and their homes.** `_transcript_str` (used by records
lines 1066, 1086, 1921) → `records.py`. `_short_test_summary` (1422) and
`_move_paths` (1361) → `merge_ladder.py`. `_context_surprise` (1673–1738),
`_is_retry` (merge ladder *and* escalation, 8 sites) and `_operator_surprise`
(merge ladder and escalation, 7 sites) → `escalating.py` (the existing
`execution/escalation.py` is the broker and policy transport and keeps that
name), imported by the merge ladder. `_spec_hash` (generation 635, re-entry 857, plus tests) →
`records.py`. `_ContentFilterStop` is raised in `_worker_call` (522, 539) and
caught in `_run_generation` (583, 591) → stays with generation.

**What the executor seam looks like.** `Executor = Callable[[GroupContext], Awaitable[GroupState]]` (`execution/scheduler.py:281`). `make_executor(deps)`
returns a closure that constructs `_GroupExecution(deps, ctx)` and awaits
`.run()`. The scheduler never sees the class. None of that changes.

**`merge.py` imports `review.py` only for `MergeConflict`** (`merge.py:21`);
`review.py` does not import `merge.py` (the merge is injected as
`deps.merge_group`). Moving `MergeConflict` into `merge.py` removes the only
backward edge from the merge implementation into the loop, and creates no
cycle: `merge.py` imports `preflight`, `worktrees`, `config`, `model` only.

**Tooling.** `ruff` is configured (`pyproject.toml:35`, line length 100,
py312); no `mypy` section exists, so the host Protocol is checked by a test,
not a type checker. Tests run with `-m "not llm"` by default
(`pyproject.toml:39`); the live tier is `-m llm` and is driver-only in a
worker sandbox. Baseline on this branch: `1935 passed, 22 deselected in 104.85s`.

**Documentation that names the file.** `docs/orchestrator-flow.md:52`
("`review.py` drives each group through coder/reviewer generations and
rounds") and the `orchestrator/execution/__init__.py` docstring. The four
`docs/2026-08-*-findings.md` files cite historical line numbers and are left
as history.

## Decisions

- **Mixins over the same `self`, plus a host Protocol.** Each responsibility
  becomes a class with no `__init__` in its own module; `_GroupExecution`
  inherits all six and keeps the only `__init__`. Every method body moves
  unchanged, so the diff is a move and reviewable as one. `orchestrator/execution/host.py`
  declares `ExecutionHost`, a `typing.Protocol` listing every attribute the
  composition root's `__init__` sets (the list under *coupling map* above),
  and each mixin declares as class-level annotations the subset it reads —
  that is the seam Plan B's `run` executor satisfies. Rejected: composition
  with an explicit state object (every method line rewritten; regressions
  hide in the noise); a plain move with no protocol (the implicit state is
  exactly what a second executor cannot see).
- **Six modules by responsibility, one composition root.** `generation.py`,
  `reviewer.py`, `records.py`, `merge_ladder.py`, `escalating.py`,
  `surprises.py`; `review.py` keeps `ReviewDeps`, `make_executor`,
  `_GroupExecution.__init__`, `_current_child`, `_on_cure`, `_log`. Rejected:
  three coarse modules (a `run` executor would import the coder loop to get
  session records).
- **No facade at the end; a transitional re-export during the moves.**
  The end state: `MergeConflict` lives in `execution/merge.py`;
  `SurpriseBoard` and the residue helpers in `execution/surprises.py`;
  `_spec_hash` in `execution/records.py`; `GroupFailure` is imported from
  `execution/scheduler.py`, where it was always defined; `review.py`
  re-exports nothing it no longer defines, and the layout test asserts that
  from U7 on. During U1–U5 each moved public name keeps a one-line
  re-export in `review.py` marked `# transitional re-export — removed in U7`,
  so the eleven test files and four package importers are untouched while
  the bodies move, and each move's diff is the move alone. U6 switches the
  tests, U7 switches the package and deletes the re-exports. Test files
  change **only their import statements**. Rejected: switching importers in
  the same unit as each move (three units priced at 2–2.5× the cap because
  the estimator reads `cli.py` and `tests/test_review_loop.py` as full
  reads); a permanent facade (nothing outside the file would prove the new
  modules are the real homes, and it lingers).
- **The two importer units are priced far above their real work, and that
  is accepted.** The estimator prices a file by its bytes, so an import-line
  edit to `cli.py` (138 KB) or the eleven test files (292 KB) prices as a
  full read: U6 and U7 show as over the cap in `group --price`. Their real
  work is a `grep -n "from orchestrator.execution.review import"` and an
  edit of that block per file; the unit prose says so and tells the worker
  not to open those files in full. `group --no-spec` accepts the plan (an
  over-cap single task is a cost flag, not a refusal); the retirement
  breaker is the backstop if a worker reads anyway.
- **Sequential units, one module per unit, each merged through Preflight
  before the next starts.** Every unit rewrites `review.py`, so they cannot
  run in parallel; chaining them by `depends_on` makes each merge a full-suite
  checkpoint and keeps each worker's diff to one responsibility. Order is by
  coupling: surprises (module-level, almost no `self`), merge ladder,
  escalation handlers, records + reviewer, generation; then the two importer
  switches. Rejected: one unit for the whole split (one 97 KB file read plus
  six writes in one context, and one giant diff).
- **Two oracles beyond the suite.** A layout test
  (`tests/test_execution_layout.py`) asserts the new modules' import graph is
  acyclic, that no two mixins define the same method name, that every
  attribute a mixin annotates is set by `_GroupExecution.__init__`, and that
  `review.py` defines none of the moved names. And the run driver executes
  the live tier (`-m llm`) after the last unit, because the risk this split
  exists to prevent — a second executor without heartbeat parity — is only
  visible with a real child.
- **`cli.py` is not split here.** It is the next refactor candidate, but it
  has no bearing on R9, and the same "one file, one plan" discipline applies.

No ADR: the decisions are reversible file moves.

## Units

### U1. surprises — SurpriseBoard, residue reporting and the prompt-note channel move to `execution/surprises.py`, with the host Protocol and the layout test that guard every later move

- **Summary**: `orchestrator/execution/surprises.py` now owns `SurpriseBoard`, `surprise_residue`, `format_residue_report`, the `REASON_*` constants and a `SurpriseHandling` mixin (`_spread`, `_handle_pending_surprises`, `_apply_briefing`, `_apply_env_notice`, `_apply_operator_note`); `orchestrator/execution/host.py` declares the `ExecutionHost` Protocol every mixin annotates against; `tests/test_execution_layout.py` checks the layout mechanically; `review.py` keeps transitional re-exports of the moved public names until U7.
- **Goal**: The surprise board, its residue helpers and the three prompt-note appliers live in `surprises.py`; `_GroupExecution` inherits `SurpriseHandling`; `review.py` re-exports `SurpriseBoard`, `surprise_residue`, `format_residue_report` and the `REASON_*` constants with the comment `# transitional re-export — removed in U7`, so no importer changes in this unit. `host.py` declares `ExecutionHost` with every attribute `_GroupExecution.__init__` sets, and `SurpriseHandling` annotates the ones it reads (`gid`, `group`, `deps`, `_briefing_notes`, `_operator_notes`, `_env_failure`, `_log`, `_rewrite`). The layout test lands here so every later unit inherits it.
- **Files**: `orchestrator/execution/review.py`, `orchestrator/execution/surprises.py` *(new, large)*, `orchestrator/execution/host.py` *(new, small)*, `tests/test_execution_layout.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `Run: uv run pytest -q -p no:cacheprovider tests/test_surprise_board.py tests/test_finish.py tests/test_review_scratch.py tests/test_review_loop.py tests/test_execution_layout.py` — `Pass:` all pass with zero edits to any pre-existing test file (`git diff main --stat -- tests/` lists only `tests/test_execution_layout.py`).
  - `Run: uv run pytest -q -p no:cacheprovider -m "not llm"` — `Pass:` `1935 passed` (the baseline count) plus the new layout tests, zero failures.
  - `Run: uv run python -c "import orchestrator.execution.surprises as s; s.SurpriseBoard; s.surprise_residue; s.format_residue_report; s.REASON_UNKNOWN_GROUP; s.SurpriseHandling"` — `Pass:` exits 0 (the new module is the definition site).
  - `Run: uv run pytest -q tests/test_execution_layout.py` — `Pass:` the layout test asserts (a) `orchestrator.execution.{surprises,host,review}` import with no cycle, (b) every class-level annotation on `SurpriseHandling` names an attribute of `ExecutionHost`, (c) every `ExecutionHost` attribute is assigned in `_GroupExecution.__init__` (checked by instantiating through `tests/test_review_loop.py`'s `Harness` and inspecting `vars()`), (d) no method name is defined on more than one mixin in `_GroupExecution.__mro__`.
  - `Run: uv run ruff check orchestrator/execution tests/test_execution_layout.py` — `Pass:` clean.
  - `Run: uv run smart-mcps-orchestrate finish --help` — `Pass:` prints usage (the `finish` command imports `surprise_residue` at module load through the re-export; a broken import fails here before any run does).
- **Edge cases**: —
- **Non-goals / must-not**: must not change any string the surprise board writes to disk or logs; must not reorder `SurpriseBoard._persist` output; must not edit any importer outside `review.py`.

### U2. merge-ladder — the merge gate, untracked ladder, flake re-run and conflict resolution move to `execution/merge_ladder.py`; `MergeConflict` moves to `execution/merge.py`

- **Summary**: `orchestrator/execution/merge_ladder.py` owns the `MergeLadder` mixin (`_merge`, `_log_remerge`, `_should_rerun_for_flake`, `_handle_untracked`, `_archive_coder_scratch`, `_classify_preflight`, `_resolve_conflict_in_place`, `_log_driver_run_items`, helpers `_short_test_summary`, `_move_paths`), and `MergeConflict` is defined in `orchestrator/execution/merge.py`, which no longer imports the loop; `review.py` re-exports `MergeConflict` transitionally.
- **Goal**: The merge path is one module a future executor can reuse or skip. `merge.py` defines `MergeConflict` and drops its import of `review.py`; `merge_ladder.py` and `review.py` import it from `merge.py`, and `review.py` re-exports it with the transitional comment so `cli.py` and the tests are untouched. `MergeLadder` annotates the host attributes it reads (`deps`, `ctx`, `group`, `gid`, `generation`, `workspace`, `coder_sid`, `coder_entry`, `_flake_reruns`, `_untracked_strikes`, `_heartbeat`, `_log`, `_escalate`, `_rewrite`, `_relaunch`, `_spread`, `_persist_coder_usage`, `_make_coder_on_turn`). `_is_retry` and `_operator_surprise` are still imported from `review.py` in this unit (they move with the escalation handlers in U3).
- **Files**: `orchestrator/execution/review.py`, `orchestrator/execution/merge_ladder.py` *(new, large)*, `orchestrator/execution/merge.py`, `orchestrator/execution/host.py`, `tests/test_execution_layout.py`
- **Symbols**: —
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `Run: uv run pytest -q -p no:cacheprovider tests/test_merge.py tests/test_preflight.py tests/test_merge_gate_triage.py tests/test_review_loop.py tests/test_execution_layout.py` — `Pass:` all pass with zero edits to any pre-existing test file.
  - `Run: uv run pytest -q -p no:cacheprovider -m "not llm"` — `Pass:` baseline count plus layout tests, zero failures.
  - `Run: uv run python -c "import orchestrator.execution.merge as m; assert m.MergeConflict.__module__ == 'orchestrator.execution.merge'"` — `Pass:` exits 0 (defined in `merge.py`, not merely imported there).
  - `Run: uv run python -c "import orchestrator.execution.merge, sys; assert 'orchestrator.execution.review' not in sys.modules"` — `Pass:` exits 0 (importing the merge implementation no longer loads the loop).
  - `Run: uv run pytest -q tests/test_execution_layout.py` — `Pass:` `MergeLadder` is in the layout test's mixin list and all four layout assertions hold.
  - `Run: uv run ruff check orchestrator/execution` — `Pass:` clean.
- **Edge cases**: —
- **Non-goals / must-not**: must not change the untracked ladder's strike count, the flake re-run count, or any log line the merge gate emits; must not edit any importer outside `review.py` and `merge.py`.

### U3. escalating — escalation, approval gates, the coder-question channel, rewrite and relaunch move to `execution/escalating.py`

- **Summary**: `orchestrator/execution/escalating.py` owns the `EscalationHandlers` mixin (`_escalate`, `_approve_gate`, `_diff`, `_resolve_needs_input`, `_on_content_filtered`, `_on_coder_stuck`, `_on_reviewer_hard`, `_rewrite`, `_relaunch`, `_persist_rewritten_spec`, `_decisions_text`, `_advance_generation`) and the helpers `_context_surprise`, `_is_retry`, `_operator_surprise`, which the merge ladder now imports from it; the existing `execution/escalation.py` (broker and policy transport) keeps its name and is not touched.
- **Goal**: Every path that raises an escalation, answers one, or spends a rewrite or relaunch is one module, so a second executor gets the Operator Decision ledger and the HITL gates by inheriting one class. `EscalationHandlers` annotates the host attributes it reads (`deps`, `ctx`, `group`, `gid`, `generation`, `workspace`, `coder_sid`, `coder_entry`, `handoff_prompt`, `rewrites`, `sessions_spawned`, `_questions`, `_operator_notes`, `_current_round_no`, `_heartbeat`, `_log`, `_round_tag`, `_spread`, `_worker_call`, `_make_coder_on_turn`). `merge_ladder.py` imports `_is_retry` and `_operator_surprise` from `escalating.py`.
- **Files**: `orchestrator/execution/review.py`, `orchestrator/execution/escalating.py` *(new, large)*, `orchestrator/execution/merge_ladder.py`, `orchestrator/execution/host.py`, `tests/test_execution_layout.py`
- **Symbols**: —
- **Depends-on**: U2
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `Run: uv run pytest -q -p no:cacheprovider tests/test_review_loop.py tests/test_merge_gate_triage.py tests/test_review_recovery_log.py tests/test_execution_layout.py` — `Pass:` all pass with zero edits to any pre-existing test file.
  - `Run: uv run pytest -q -p no:cacheprovider -m "not llm"` — `Pass:` baseline count plus layout tests, zero failures.
  - `Run: uv run python -c "import orchestrator.execution.escalating as e; e.EscalationHandlers; e._is_retry; e._operator_surprise; e._context_surprise"` — `Pass:` exits 0.
  - `Run: uv run pytest -q tests/test_execution_layout.py` — `Pass:` `EscalationHandlers` is in the mixin list; the import graph over `execution/{surprises,merge_ladder,escalating,host,review}.py` is acyclic (`merge_ladder → escalating` is the one allowed inter-mixin edge and the test names it).
  - `Run: uv run smart-mcps-orchestrate answer --help` — `Pass:` prints usage (the answer path imports the escalation broker and ledger the handlers depend on).
  - `Run: uv run ruff check orchestrator/execution` — `Pass:` clean.
- **Edge cases**: —
- **Non-goals / must-not**: must not change the order in which `_escalate` writes the request file, logs, and awaits the broker; must not change any `EscalationKind` routing; must not touch `execution/escalation.py`.

### U4. records-and-reviewer — session records, usage and transcript bookkeeping move to `execution/records.py`; the reviewer round moves to `execution/reviewer.py`

- **Summary**: `orchestrator/execution/records.py` owns the `SessionRecords` mixin (`_record`, `_copy_usage`, `_persist_coder_usage`, `_persist_reviewer_usage`, `_make_coder_on_turn`, `_adopt_actual_session_id`, `_refresh_transcript`, `_watch_transcript`, `_round_tag`, `_log_coder_session_end`) plus `_transcript_str` and `_spec_hash`; `orchestrator/execution/reviewer.py` owns the `ReviewerRound` mixin (`_review_round`, `_archive_review_scratch`); `review.py` re-exports `_spec_hash` transitionally.
- **Goal**: Session bookkeeping — the manifest entries, per-session usage, transcript watching and the round tag — is a module any executor can inherit without the coder loop, and the reviewer ferry is its own module. `review.py` re-exports `_spec_hash` with the transitional comment so `tests/test_review_loop.py` is untouched. `SessionRecords` annotates (`deps`, `gid`, `generation`, `workspace`, `coder_sid`, `coder_entry`, `sessions_spawned`, `_current_round_no`, `_heartbeat`, `_log`); `ReviewerRound` annotates (`deps`, `group`, `gid`, `generation`, `workspace`, `reviewer_sid`, `extra_pass_done`, `_log`, `_round_tag`, `_record`, `_spread`, `_worker_call`, `_launch_call`, `_decisions_text`, `_persist_reviewer_usage`).
- **Files**: `orchestrator/execution/review.py`, `orchestrator/execution/records.py` *(new, medium)*, `orchestrator/execution/reviewer.py` *(new, medium)*, `orchestrator/execution/host.py`, `tests/test_execution_layout.py`
- **Symbols**: —
- **Depends-on**: U3
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `Run: uv run pytest -q -p no:cacheprovider tests/test_review_loop.py tests/test_review_scratch.py tests/test_liveness_wiring.py tests/test_suspend_cure.py tests/test_execution_layout.py` — `Pass:` all pass with zero edits to any pre-existing test file.
  - `Run: uv run pytest -q -p no:cacheprovider -m "not llm"` — `Pass:` baseline count plus layout tests, zero failures.
  - `Run: uv run python -c "from orchestrator.execution.records import _spec_hash, SessionRecords; from orchestrator.execution.reviewer import ReviewerRound; assert _spec_hash.__module__ == 'orchestrator.execution.records'"` — `Pass:` exits 0.
  - `Run: uv run pytest -q tests/test_execution_layout.py` — `Pass:` both new mixins are in the list; every annotated attribute is set by `_GroupExecution.__init__`; no duplicate method names across the five mixins now present.
  - `Run: uv run ruff check orchestrator/execution` — `Pass:` clean.
- **Edge cases**: —
- **Non-goals / must-not**: must not change the session display names, the artifact names (`artifact_name("report"|"verdict", gen, round)`), or the usage fields copied into `SessionEntry`.

### U5. generation — the generation lifecycle moves to `execution/generation.py`, `review.py` becomes the composition root, and the docs describe the layout

- **Summary**: `orchestrator/execution/generation.py` owns the `GenerationLoop` mixin (`run`, `_run_generation`, `_run_generation_body`, `_find_reentry_session`, `_reenter`, `_launch_call`, `_reentry_fallback`, `_worker_call`, `_breaker_reason`, `_retire`, `_prepare_handoff`, and `_ContentFilterStop`); `orchestrator/execution/review.py` is left with `ReviewDeps`, `make_executor`, the transitional re-exports, and `_GroupExecution` composed of the six mixins with only `__init__`, `_current_child`, `_on_cure` and `_log`; `docs/orchestrator-flow.md` and the `execution/__init__.py` docstring describe the layout.
- **Goal**: `review.py` is under 400 lines and reads as the composition root of today's coder-and-reviewer machine: the dependency record, the factory, the `__init__` that births every attribute `ExecutionHost` lists, and the class line `class _GroupExecution(GenerationLoop, ReviewerRound, SessionRecords, MergeLadder, EscalationHandlers, SurpriseHandling)`. `docs/orchestrator-flow.md` gains a short "execution modules" table (one row per module, what it owns, which host attributes it reads) replacing the single `review.py` sentence at line 52; the `execution/__init__.py` docstring lists the modules.
- **Files**: `orchestrator/execution/review.py`, `orchestrator/execution/generation.py` *(new, large)*, `orchestrator/execution/host.py`, `orchestrator/execution/__init__.py`, `docs/orchestrator-flow.md`, `tests/test_execution_layout.py`
- **Symbols**: —
- **Depends-on**: U4
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `Run: uv run pytest -q -p no:cacheprovider -m "not llm"` — `Pass:` baseline count plus layout tests, zero failures, and `git diff main --stat -- tests/` still lists only `tests/test_execution_layout.py`.
  - `Run: wc -l orchestrator/execution/review.py` — `Pass:` under 400 lines, and `grep -c "    def \|    async def " orchestrator/execution/review.py` reports at most 5 (the `__init__`, `_current_child`, `_on_cure`, `_log` and the `executor` closure).
  - `Run: uv run pytest -q tests/test_execution_layout.py` — `Pass:` all six mixins are in the list; the import graph over the seven `execution/` modules is acyclic; every `ExecutionHost` attribute is set in `__init__`; no duplicate method names across mixins.
  - `Run: uv run pytest -q -p no:cacheprovider tests/test_e2e_stub.py tests/test_suspend_cure.py tests/test_liveness_wiring.py` — `Pass:` the stub end-to-end run, the Suspend Cure path and the liveness wiring pass unchanged (these exercise heartbeat, probe, and generation retirement through `make_executor`).
  - `Run: uv run ruff check orchestrator/execution && uv run mdformat --check docs/orchestrator-flow.md` — `Pass:` both clean.
  - `Run: grep -n "review.py" docs/orchestrator-flow.md` — `Pass:` the line describes `review.py` as the composition root and the table names all six modules.
- **Edge cases**: —
- **Non-goals / must-not**: must not rename `review.py`, `ReviewDeps` or `make_executor`; must not remove the transitional re-exports (U7 does); must not change the MRO-visible behaviour (no mixin overrides another's method; the layout test enforces it).

### U6. test-importers — the eleven test files import every moved name from its new home, touching import statements only

- **Summary**: The eleven test files that imported from `orchestrator/execution/review.py` now import `SurpriseBoard`, `surprise_residue`, `format_residue_report` and `REASON_*` from `execution/surprises.py`, `MergeConflict` from `execution/merge.py`, `_spec_hash` from `execution/records.py`, and `GroupFailure` from `execution/scheduler.py`; only import statements change.
- **Goal**: Every test imports each name from its definition site, so the transitional re-exports have no test consumer left. The worker locates each block with `grep -n "from orchestrator.execution.review import" tests/` and edits that block and nothing else; it does not open these files in full (the estimator prices this unit as a full read of 292 KB of tests — the real work is eleven import blocks). `ReviewDeps`, `make_executor` and `_GroupExecution` keep importing from `review.py`, which remains their home.
- **Files**: `tests/test_finish.py`, `tests/test_review_scratch.py`, `tests/test_suspend_cure.py`, `tests/test_scheduler.py`, `tests/test_review_loop.py`, `tests/test_surprise_board.py`, `tests/test_merge.py`, `tests/test_liveness_wiring.py`, `tests/test_review_recovery_log.py`, `tests/test_preflight.py`, `tests/test_merge_gate_triage.py`
- **Symbols**: —
- **Depends-on**: U5
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `Run: uv run pytest -q -p no:cacheprovider -m "not llm"` — `Pass:` baseline count plus layout tests, zero failures.
  - `Run: git diff main -U0 -- tests/ ':!tests/test_execution_layout.py' | grep '^[+-]' | grep -v '^[+-][+-]' | grep -vE '^[+-](from |import |\s+[A-Za-z_]+,?$|\)$)'` — `Pass:` prints nothing (every changed line in a pre-existing test file is part of an import statement).
  - `Run: grep -rn "from orchestrator.execution.review import" tests/ | grep -vE "ReviewDeps|make_executor|_GroupExecution"` — `Pass:` prints nothing (no test imports a moved name from `review.py`).
  - `Run: grep -rn "GroupFailure" tests/ | grep "execution.review"` — `Pass:` prints nothing (`GroupFailure` comes from `scheduler.py`).
  - `Run: uv run ruff check tests` — `Pass:` clean (import blocks re-sorted where ruff's isort rule applies).
- **Edge cases**: —
- **Non-goals / must-not**: must not change any test body, fixture, or assertion; must not touch `orchestrator/`.

### U7. package-importers — `cli.py`, `finish.py`, `observatory/runs.py` import from the new homes, the transitional re-exports are deleted, and the live tier proves nothing changed

- **Summary**: `orchestrator/cli.py`, `orchestrator/execution/finish.py` and `orchestrator/observatory/runs.py` import the moved names from `execution/surprises.py` and `execution/merge.py`; `review.py` drops every `# transitional re-export` line and the layout test now asserts it defines none of the moved names; the run driver executes the live tier as the final oracle.
- **Goal**: The end state the decisions describe: `review.py` defines `ReviewDeps`, `make_executor` and `_GroupExecution` and nothing that lives elsewhere. The worker edits the import block in each of the three package files (located with `grep -n "from orchestrator.execution.review import"`) and does not open `cli.py` in full (the estimator prices it as a 138 KB read; the real edit is one import block). The layout test gains the assertion that `orchestrator.execution.review` has no attribute named `SurpriseBoard`, `surprise_residue`, `format_residue_report`, `REASON_UNKNOWN_GROUP`, `MergeConflict` or `_spec_hash`.
- **Files**: `orchestrator/execution/review.py`, `orchestrator/cli.py`, `orchestrator/execution/finish.py`, `orchestrator/observatory/runs.py`, `tests/test_execution_layout.py`
- **Symbols**: —
- **Depends-on**: U6
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `Run: uv run pytest -q -p no:cacheprovider -m "not llm"` — `Pass:` baseline count plus layout tests, zero failures.
  - `Run: grep -n "transitional re-export" orchestrator/execution/review.py` — `Pass:` prints nothing.
  - `Run: uv run pytest -q tests/test_execution_layout.py` — `Pass:` the no-re-export assertion holds alongside the four layout assertions.
  - `Run: uv run smart-mcps-orchestrate status --help && uv run smart-mcps-orchestrate finish --help && uv run python -c "import orchestrator.observatory.runs"` — `Pass:` all three import cleanly at module load.
  - `Run: git diff main -U0 -- orchestrator/cli.py orchestrator/execution/finish.py orchestrator/observatory/runs.py | grep '^[+-]' | grep -v '^[+-][+-]' | grep -vE '^[+-](from |import |\s+[A-Za-z_]+,?$|\)$)'` — `Pass:` prints nothing (import statements only in the three package files).
  - `Run (driver): uv run pytest -q -m llm` — `Pass:` every live-tier test passes with a real `claude` child; the driver records the command output as evidence. This is the external oracle for heartbeat parity and cannot run in a worker sandbox.
  - `Run: uv run ruff check orchestrator tests` — `Pass:` clean.
- **Edge cases**: —
- **Non-goals / must-not**: must not change any function body in the three package files; must not rename `review.py`; must not narrow the driver-run live tier — `Run (driver): uv run pytest -q -m llm` runs the full `-m llm` set (all five `tests/test_*_live.py` and `tests/test_grouping_llm.py`), not only the heartbeat-parity files (decided 2026-09-23).

<!-- deepened 2026-09-23 (sandbox sweep) against plan-content c28b679e3e79 -->

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-surprises
    description: SurpriseBoard, residue reporting and the prompt-note channel move to execution/surprises.py, with the host Protocol and the layout test that guard every later move
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/execution/surprises.py
      - orchestrator/execution/host.py
      - tests/test_execution_layout.py
    size_hints:
      orchestrator/execution/surprises.py: large
      orchestrator/execution/host.py: small
      tests/test_execution_layout.py: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u2-merge-ladder
    description: The merge gate, untracked ladder, flake re-run and conflict resolution move to execution/merge_ladder.py; MergeConflict moves to execution/merge.py
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/execution/merge_ladder.py
      - orchestrator/execution/merge.py
      - orchestrator/execution/host.py
      - tests/test_execution_layout.py
    size_hints:
      orchestrator/execution/merge_ladder.py: large
    symbols: []
    depends_on: [u1-surprises]
    implements: []
    consumes: []
  - task_id: u3-escalating
    description: Escalation, approval gates, the coder-question channel, rewrite and relaunch move to execution/escalating.py
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/execution/escalating.py
      - orchestrator/execution/merge_ladder.py
      - orchestrator/execution/host.py
      - tests/test_execution_layout.py
    size_hints:
      orchestrator/execution/escalating.py: large
    symbols: []
    depends_on: [u2-merge-ladder]
    implements: []
    consumes: []
  - task_id: u4-records-and-reviewer
    description: Session records, usage and transcript bookkeeping move to execution/records.py; the reviewer round moves to execution/reviewer.py
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/execution/records.py
      - orchestrator/execution/reviewer.py
      - orchestrator/execution/host.py
      - tests/test_execution_layout.py
    size_hints:
      orchestrator/execution/records.py: medium
      orchestrator/execution/reviewer.py: medium
    symbols: []
    depends_on: [u3-escalating]
    implements: []
    consumes: []
  - task_id: u5-generation
    description: The generation lifecycle moves to execution/generation.py, review.py becomes the composition root, and the docs describe the layout
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/execution/generation.py
      - orchestrator/execution/host.py
      - orchestrator/execution/__init__.py
      - docs/orchestrator-flow.md
      - tests/test_execution_layout.py
    size_hints:
      orchestrator/execution/generation.py: large
    symbols: []
    depends_on: [u4-records-and-reviewer]
    implements: []
    consumes: []
  - task_id: u6-test-importers
    description: The eleven test files import every moved name from its new home, touching import statements only
    slice: null
    files:
      - tests/test_finish.py
      - tests/test_review_scratch.py
      - tests/test_suspend_cure.py
      - tests/test_scheduler.py
      - tests/test_review_loop.py
      - tests/test_surprise_board.py
      - tests/test_merge.py
      - tests/test_liveness_wiring.py
      - tests/test_review_recovery_log.py
      - tests/test_preflight.py
      - tests/test_merge_gate_triage.py
    symbols: []
    depends_on: [u5-generation]
    implements: []
    consumes: []
  - task_id: u7-package-importers
    description: cli.py, finish.py and observatory/runs.py import from the new homes, the transitional re-exports are deleted, and the live tier proves nothing changed
    slice: null
    files:
      - orchestrator/execution/review.py
      - orchestrator/cli.py
      - orchestrator/execution/finish.py
      - orchestrator/observatory/runs.py
      - tests/test_execution_layout.py
    symbols: []
    depends_on: [u6-test-importers]
    implements: []
    consumes: []
```
