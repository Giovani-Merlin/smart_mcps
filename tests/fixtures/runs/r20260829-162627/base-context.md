# Base context

## Worker ground rules

These rules apply to every coder and reviewer session forked from this base
context, for the spec you will be given below and the plan document that
follows it.

### For coders

- Work only inside your worktree (your current working directory); never touch
  paths outside it.
- Your worktree owns its own environment: dependency changes require `uv sync`
  run inside the worktree, and any verification item that imports a new
  dependency must pass there, in that worktree — never against the parent
  checkout's environment.
- Implement the spec you are given fully — code and tests — following the
  conventions established above.
- Commit early and often: after each self-contained step that leaves the
  worktree in a consistent state (a finished file, a passing unit of work),
  make a git commit with a clear conventional message. Do not accumulate large
  uncommitted work — if your session is interrupted, only committed work
  survives; anything uncommitted is lost when the group restarts. The commit
  subject must start with the first character of the type, not whitespace — if
  you're using a heredoc to pass the message, check the exact bytes, since a
  leading newline or space before the subject line is a common heredoc mistake.
- If a command is denied for permissions, retry the *identical* command up to
  three times total, then stop and report status `permission_denied` with the
  denied command verbatim in `denied_command`. Re-sending the identical command
  is not a workaround; alternate quoting, alternate spellings, shelling through
  another interpreter, and `subprocess.run` substitution for the same command
  are all banned — they route around the sandbox instead of reporting the block.
- Verify your work against the verification items you will be given before
  reporting.

### For reviewers

- Compute the diff yourself from git rather than trusting the coder's report:
  verify claims against the actual code and run the tests the spec calls for.
- If you need scratch space, use only the directory the round names for it, and
  do not leave scratch files anywhere else in the worktree — the merge gate
  requires a clean tree.

### Report block rules (both roles)

Every final message ends with EXACTLY ONE `<run-report>` block, whose body is
valid JSON with no trailing commas and no comments, and nothing after the
closing tag.

- A coder's `status` attribute is one of completed | blocked | failed |
  needs_input | permission_denied and must match the JSON body's `"status"`
  field.
  - Use `needs_input` only when a decision only a human can make blocks you (an
    ambiguous requirement, a product trade-off, missing access). Put the single
    specific question in a top-level `"question"` field; the run pauses and
    resumes you with the operator's answer. Do not use it for anything you can
    resolve yourself.
  - Use `permission_denied` only after retrying the identical denied command up
    to three times total. Put the exact command in a top-level
    `"denied_command"` field, verbatim, with no paraphrasing or quoting
    changes. Do not use it for a `blocked` report — that status is unrelated to
    permission denials.
  - With `permission_denied`, also set two top-level fields that say *how* it
    was denied, because three unrelated causes look identical from where you
    stand and each needs a different fix from the operator:
    - `"denial_error"`: the error text you actually saw, **verbatim** — do not
      summarize or rephrase it. If nothing came back at all, write exactly
      `no error text was returned`; a stated absence is usable, a blank field
      is not.
    - `"denial_source"`: `"tool_refused"` if the tool call was refused and the
      command never ran, or `"command_error"` if the command ran and failed.
      You know which one happened and the orchestrator cannot work it out
      afterwards, so this is the single most useful thing you can report here.
  - Every verification item you were given must appear in
    `"verification_results"` with status pass | fail | skipped.
- A reviewer's `status` attribute is one of approved | changes_required |
  too_hard | structural and must match the JSON body's `"status"` field.
  - approved: the work satisfies the spec and its verification items.
  - changes_required: fixable within this group — list concrete, actionable
    items in `"required_changes"`.
  - too_hard: the spec cannot be satisfied by iterating here; escalate for a
    spec rewrite.
  - structural: the group boundaries themselves are wrong (work belongs to or
    conflicts with another group).
- Either role may record a surprise: a finding that likely invalidates another
  group's assignment — an interface mismatch, a missing dependency, work that
  belongs elsewhere. Record it instead of fixing it yourself:
  `{"kind": "interface_mismatch" | "missing_dependency" | "merge_conflict" | "other", "description": "...", "affected_groups": ["g2"]}`

## Repo conventions (CLAUDE.md)

# smart-mcps

A Claude Code plugin that installs skills and session hooks for codegraph, NotebookLM, and Perplexity.

## Plugin structure

This is a **Claude Code plugin repository**. Hooks and skills are distributed to plugin consumers via auto-discovered files:

| Path                         | Purpose                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| `hooks/hooks.json`           | Plugin-level hook registrations (uses `${CLAUDE_PLUGIN_ROOT}`)                       |
| `hooks/scripts/`             | Hook implementation scripts (`.py` and `.sh`)                                        |
| `skills/`                    | Skill definitions                                                                    |
| `agents/`                    | Subagent definitions (auto-discovered; `.claude/agents` symlinks here for local dev) |
| `codegraph_mcp/`             | FastMCP proxy exposing 6 trimmed codegraph tools (`smart-mcps-codegraph`)            |
| `.mcp.json`                  | MCP server registration — serves plugin consumers **and** local dev (see below)      |
| `.claude-plugin/plugin.json` | Plugin identity (name, version)                                                      |
| `.claude/settings.json`      | Project-level hooks for local development (uses `$CLAUDE_PROJECT_DIR`)               |

### Adding hooks — always register in both places

Every hook script must be wired up in **two** files:

1. **`hooks/hooks.json`** — so plugin consumers receive it. Uses `${CLAUDE_PLUGIN_ROOT}`.
2. **`.claude/settings.json`** — so it runs locally during development. Uses `$CLAUDE_PROJECT_DIR`.

Matchers are **case-sensitive** and PascalCase in **both** files (e.g. `"Edit|Write|MultiEdit"`, `"Bash"`) — a lowercase matcher silently matches nothing. This applies to every event, including `SessionStart` (codegraph reindex, formatter bootstrap), not just `PostToolUse`.

Registering in only one place means either plugin consumers or local dev is broken. Always do both.

### MCP servers — one file, not two

The dual-registration rule above does **not** apply to MCP servers: `.claude/settings.json` has no `mcpServers` key (it only *approves* servers via `enabledMcpjsonServers`). A single `.mcp.json` at the repo root covers both audiences, because Claude Code reads it twice:

- **Plugin consumers** — auto-discovered as the plugin's `./.mcp.json`; tools resolve as `mcp__plugin_smart-mcps_codegraph__<tool>`.
- **Local dev** — read as the project-scoped `.mcp.json`; tools resolve as `mcp__codegraph__<tool>`.

Hence `"--project", "${CLAUDE_PLUGIN_ROOT:-.}"`. `${CLAUDE_PLUGIN_ROOT}` is set only for consumers; locally it falls back to `.` (the server's cwd is the project root). Two constraints worth knowing before editing that line:

- **`${CLAUDE_PROJECT_DIR}` is not available in `.mcp.json`** — only in hooks. Using it yields a "Missing environment variables" warning.
- **Defaults don't nest.** `${VAR:-default}` works, but `${A:-${B}}` does not expand `B` — it silently passes a literal, which `uv` may accept when the console script is already on PATH, hiding the bug locally while breaking for consumers.

## Never keep working notes in the `/tmp` scratchpad

The session scratchpad under `/tmp` is **wiped when the Claude Code process restarts** — not
just at the end of a session. Anything written there is gone without warning.

This has already cost real work: on 2026-07-29 a restart destroyed the accumulated findings
and deferred-fix list for two sessions of orchestrator debugging, plus a patch backup of a
group's stranded work.

Use `/tmp` only for genuinely throwaway intermediates. Anything another session would want —
findings, deferred-fix lists, handoff notes, backups of uncommitted work — goes somewhere
durable:

| what                                  | where                                                                               |
| ------------------------------------- | ----------------------------------------------------------------------------------- |
| Run findings, deferred fixes, backups | `.orchestrator/` — gitignored, survives restarts, never collides with a group merge |
| Facts worth recalling across sessions | the auto-memory dir (`~/.claude/projects/<project>/memory/`)                        |
| Anything the repo should carry        | `docs/` — committed                                                                 |

Write it durably **as you go**, not at the end. A restart gives no notice.

## Codebase architecture (codegraph)

Project Structure (191 files):

├── codegraph_mcp
│   ├── __init__.py (python, 1 symbols)
│   └── server.py (python, 22 symbols)
├── hooks
│   └── scripts
│       ├── lint_after_edit.py (python, 12 symbols)
│       └── save_research.py (python, 14 symbols)
├── orchestrator
│   ├── execution
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── auth.py (python, 24 symbols)
│   │   ├── calibrate.py (python, 15 symbols)
│   │   ├── confinement.py (python, 56 symbols)
│   │   ├── denial.py (python, 16 symbols)
│   │   ├── driver.py (python, 27 symbols)
│   │   ├── escalation.py (python, 23 symbols)
│   │   ├── export.py (python, 31 symbols)
│   │   ├── finish.py (python, 30 symbols)
│   │   ├── heartbeat.py (python, 31 symbols)
│   │   ├── manifest.py (python, 51 symbols)
│   │   ├── merge.py (python, 19 symbols)
│   │   ├── preflight.py (python, 43 symbols)
│   │   ├── prompting.py (python, 24 symbols)
│   │   ├── ratelimit.py (python, 46 symbols)
│   │   ├── retry.py (python, 17 symbols)
│   │   ├── review.py (python, 88 symbols)
│   │   ├── scheduler.py (python, 75 symbols)
│   │   ├── sessions.py (python, 69 symbols)
│   │   ├── streaming.py (python, 33 symbols)
│   │   └── worktrees.py (python, 42 symbols)
│   ├── grouping
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── advisory.py (python, 45 symbols)
│   │   ├── assembler.py (python, 19 symbols)
│   │   ├── base_context.py (python, 6 symbols)
│   │   ├── errors.py (python, 7 symbols)
│   │   ├── estimator.py (python, 25 symbols)
│   │   ├── graphing.py (python, 76 symbols)
│   │   ├── llm_record.py (python, 14 symbols)
│   │   ├── llm.py (python, 39 symbols)
│   │   ├── mapper.py (python, 11 symbols)
│   │   ├── partition.py (python, 66 symbols)
│   │   ├── pipeline.py (python, 56 symbols)
│   │   ├── plan_reader.py (python, 23 symbols)
│   │   ├── plan_sections.py (python, 23 symbols)
│   │   ├── scorecard.py (python, 10 symbols)
│   │   └── trace.py (python, 46 symbols)
│   ├── observatory
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── app.py (python, 25 symbols)
│   │   ├── artifacts.py (python, 36 symbols)
│   │   ├── escalations.py (python, 14 symbols)
│   │   ├── events.py (python, 24 symbols)
│   │   ├── grouping.py (python, 53 symbols)
│   │   ├── launch.py (python, 73 symbols)
│   │   ├── paths.py (python, 26 symbols)
│   │   ├── registry.py (python, 13 symbols)
│   │   ├── runs.py (python, 48 symbols)
│   │   └── transcripts.py (python, 24 symbols)
│   ├── prompts
│   │   └── __init__.py (python, 3 symbols)
│   ├── __init__.py (python, 1 symbols)
│   ├── cli.py (python, 115 symbols)
│   ├── config.py (python, 26 symbols)
│   └── model.py (python, 32 symbols)
├── pplx
│   ├── __init__.py (python, 1 symbols)
│   └── cli.py (python, 25 symbols)
├── scripts
│   └── measure_fork_cache.py (python, 14 symbols)
├── tests
│   ├── fake_claude.py (python, 25 symbols)
│   ├── regenerate_golden_partitions.py (python, 12 symbols)
│   ├── test_advisory.py (python, 38 symbols)
│   ├── test_assembler.py (python, 21 symbols)
│   ├── test_auth_ladder.py (python, 42 symbols)
│   ├── test_calibrate.py (python, 20 symbols)
│   ├── test_cli_price.py (python, 31 symbols)
│   ├── test_cli.py (python, 176 symbols)
│   ├── test_codegraph_server.py (python, 27 symbols)
│   ├── test_confinement.py (python, 44 symbols)
│   ├── test_cwd_contract.py (python, 20 symbols)
│   ├── test_denial.py (python, 14 symbols)
│   ├── test_driver_liveness.py (python, 31 symbols)
│   ├── test_e2e_faults.py (python, 25 symbols)
│   ├── test_e2e_live.py (python, 35 symbols)
│   ├── test_e2e_stub.py (python, 47 symbols)
│   ├── test_edge_provenance.py (python, 46 symbols)
│   ├── test_escalation.py (python, 58 symbols)
│   ├── test_estimator.py (python, 25 symbols)
│   ├── test_export.py (python, 21 symbols)
│   ├── test_fingerprint_compare.py (python, 20 symbols)
│   ├── test_finish.py (python, 45 symbols)
│   ├── test_golden_partitions.py (python, 16 symbols)
│   ├── test_graphing.py (python, 62 symbols)
│   ├── test_grouper_pipeline.py (python, 128 symbols)
│   ├── test_grouping_fixtures.py (python, 60 symbols)
│   ├── test_grouping_llm.py (python, 16 symbols)
│   ├── test_grouping_trace.py (python, 45 symbols)
│   ├── test_heartbeat.py (python, 31 symbols)
│   ├── test_index_fingerprint.py (python, 26 symbols)
│   ├── test_index_quiescence.py (python, 27 symbols)
│   ├── test_informational_surprise.py (python, 10 symbols)
│   ├── test_integration_provisioning.py (python, 37 symbols)
│   ├── test_lint_after_edit.py (python, 27 symbols)
│   ├── test_llm_record.py (python, 26 symbols)
│   ├── test_llm.py (python, 18 symbols)
│   ├── test_log_event_timezone.py (python, 18 symbols)
│   ├── test_manifest_snapshot.py (python, 5 symbols)
│   ├── test_merge_gate_triage.py (python, 19 symbols)
│   ├── test_merge.py (python, 27 symbols)
│   ├── test_model_selection.py (python, 29 symbols)
│   ├── test_model.py (python, 32 symbols)
│   ├── test_nudges.py (python, 23 symbols)
│   ├── test_observatory_api.py (python, 70 symbols)
│   ├── test_observatory_diffs.py (python, 26 symbols)
│   ├── test_observatory_drift.py (python, 19 symbols)
│   ├── test_observatory_escalations.py (python, 30 symbols)
│   ├── test_observatory_events.py (python, 23 symbols)
│   ├── test_observatory_grouping.py (python, 78 symbols)
│   ├── test_observatory_launch.py (python, 63 symbols)
│   ├── test_observatory_model_drift.py (python, 26 symbols)
│   ├── test_observatory_paths.py (python, 56 symbols)
│   ├── test_observatory_spa.py (python, 30 symbols)
│   ├── test_observatory_thinking.py (python, 37 symbols)
│   ├── test_observatory_transcripts.py (python, 39 symbols)
│   ├── test_orchestrator_session_snapshot.py (python, 12 symbols)
│   ├── test_partition.py (python, 111 symbols)
│   ├── test_permission_patterns_live.py (python, 18 symbols)
│   ├── test_plan_reader.py (python, 46 symbols)
│   ├── test_plan_sections.py (python, 27 symbols)
│   ├── test_preflight_baseline.py (python, 21 symbols)
│   ├── test_preflight.py (python, 65 symbols)
│   ├── test_ratelimit.py (python, 60 symbols)
│   ├── test_resume_record.py (python, 15 symbols)
│   ├── test_retry.py (python, 27 symbols)
│   ├── test_review_loop.py (python, 146 symbols)
│   ├── test_review_recovery_log.py (python, 11 symbols)
│   ├── test_review_scratch.py (python, 33 symbols)
│   ├── test_rewrite_observability.py (python, 15 symbols)
│   ├── test_scheduler.py (python, 137 symbols)
│   ├── test_scorecard.py (python, 21 symbols)
│   ├── test_session_spend.py (python, 11 symbols)
│   ├── test_sessions.py (python, 95 symbols)
│   ├── test_specless_preview.py (python, 21 symbols)
│   ├── test_streaming_live.py (python, 15 symbols)
│   ├── test_streaming.py (python, 28 symbols)
│   ├── test_surprise_board.py (python, 22 symbols)
│   └── test_worktrees.py (python, 7 symbols)
└── ui
    ├── src
    │   ├── components
    │   │   ├── grouping
    │   │   │   ├── GroupingGraph.tsx (tsx, 13 symbols)
    │   │   │   ├── GroupingTab.test.tsx (tsx, 15 symbols)
    │   │   │   ├── GroupingTab.tsx (tsx, 33 symbols)
    │   │   │   ├── stages.test.ts (typescript, 9 symbols)
    │   │   │   └── stages.ts (typescript, 12 symbols)
    │   │   ├── launch
    │   │   │   ├── ExecutionOptions.tsx (tsx, 14 symbols)
    │   │   │   ├── GroupingPreview.tsx (tsx, 7 symbols)
    │   │   │   ├── JobLog.tsx (tsx, 9 symbols)
    │   │   │   ├── JobProgress.test.tsx (tsx, 4 symbols)
    │   │   │   └── JobProgress.tsx (tsx, 9 symbols)
    │   │   ├── AttemptGrid.test.tsx (tsx, 13 symbols)
    │   │   ├── AttemptGrid.tsx (tsx, 16 symbols)
    │   │   ├── CostPanel.test.tsx (tsx, 12 symbols)
    │   │   ├── CostPanel.tsx (tsx, 19 symbols)
    │   │   ├── DiffView.test.tsx (tsx, 7 symbols)
    │   │   ├── DiffView.tsx (tsx, 8 symbols)
    │   │   ├── EscalationPanel.tsx (tsx, 16 symbols)
    │   │   ├── EventLog.tsx (tsx, 6 symbols)
    │   │   ├── GroupBoard.test.tsx (tsx, 9 symbols)
    │   │   ├── GroupBoard.tsx (tsx, 14 symbols)
    │   │   ├── GroupDrillIn.test.tsx (tsx, 13 symbols)
    │   │   ├── GroupDrillIn.tsx (tsx, 24 symbols)
    │   │   ├── PathChip.test.tsx (tsx, 8 symbols)
    │   │   ├── PathChip.tsx (tsx, 8 symbols)
    │   │   ├── PathsDrawer.tsx (tsx, 8 symbols)
    │   │   ├── ProjectRunSwitcher.tsx (tsx, 6 symbols)
    │   │   ├── SurpriseBoard.tsx (tsx, 6 symbols)
    │   │   ├── UsageLimitBanner.test.tsx (tsx, 5 symbols)
    │   │   └── UsageLimitBanner.tsx (tsx, 7 symbols)
    │   ├── fixtures
    │   │   ├── cost-new-format.ts (typescript, 3 symbols)
    │   │   └── r20260726-grouping.ts (typescript, 3 symbols)
    │   ├── routes
    │   │   ├── JobDetail.tsx (tsx, 10 symbols)
    │   │   ├── Jobs.tsx (tsx, 8 symbols)
    │   │   ├── launch.test.tsx (tsx, 17 symbols)
    │   │   ├── Launch.tsx (tsx, 20 symbols)
    │   │   ├── ProjectIndex.tsx (tsx, 8 symbols)
    │   │   └── ProjectPicker.tsx (tsx, 4 symbols)
    │   ├── api.ts (typescript, 39 symbols)
    │   ├── App.tsx (tsx, 5 symbols)
    │   ├── attempts.test.ts (typescript, 11 symbols)
    │   ├── attempts.ts (typescript, 27 symbols)
    │   ├── cost.test.ts (typescript, 7 symbols)
    │   ├── cost.ts (typescript, 34 symbols)
    │   ├── main.tsx (tsx, 5 symbols)
    │   ├── routes.test.tsx (tsx, 11 symbols)
    │   ├── routes.tsx (tsx, 26 symbols)
    │   ├── RunLayout.tsx (tsx, 11 symbols)
    │   ├── status.test.ts (typescript, 9 symbols)
    │   ├── status.ts (typescript, 21 symbols)
    │   ├── types.ts (typescript, 76 symbols)
    │   ├── useQueryParams.ts (typescript, 5 symbols)
    │   └── useRunStream.ts (typescript, 8 symbols)
    └── vite.config.ts (typescript, 3 symbols)

## Plan digest (2026-08-29-001-feat-plan-split-and-deepen-plan.md)

---
title: Mechanical plan split and the deepen skill
type: feat
date: 2026-08-29
origin: docs/brainstorms/2026-08-28-grouper-speccer-flow-requirements.md
---

# Mechanical plan split and the deepen skill

## Objective

Ship wave 2 of the grouper-speccer-flow requirements — R16 (mechanical plan
split), R17 (`/orchestrator-deepen`), and R18 (its question policy and
enrichment template, grounded in the R19 research already received) — so that
the advisory grouper shipped in wave 1 has somewhere to lead:

- **R16**: `smart-mcps-orchestrate split` turns a chosen seam into N plan
  documents by moving unit sections and task-map entries **verbatim**, with no
  LLM regeneration of any prose.
- **R17/R18**: `/orchestrator-deepen <plan>` is a standalone interactive skill
  that spawns read-only explorers, grills the human with a capped, EVPI-ranked
  question set, and writes the answers back into the plan as per-unit
  enrichment — edge cases, non-goals, and sharpened `Run:`/`Pass:` verification
  items.

Both commands write into a plan document, which is the fingerprint-drift bug
class this repo has been bitten by before. So both go through **one shared,
tested plan-surgery module** whose guarantee is byte-level: the task map and
unit ids either survive a rewrite untouched, or the rewrite is refused.

R19 is already satisfied — both Perplexity reports were received 2026-08-28 and
their findings are folded into the origin brainstorm; this plan consumes them
rather than re-commissioning them. R20 (the eval harness) stays out of scope,
blocked on Infinity Skills ingestion.

## What we already know (resolved context)

Ground truth verified 2026-08-29 against `main` at `8adabbf`, with the full
suite green (`uv run pytest` → 1505 passed, 20 deselected).

### Wave 1 landed, but has never driven a live worker

All 13 wave-1 units are merged (PR #5, groups g1–g11). But the run that *built*
wave 1 (`.orchestrator/runs/r20260828-220035`) still used the old LLM speccer —
`llm/01-speccer_output-a0.raw.txt` exists and that run's `groups.json` specs are
LLM prose. So assembled specs (U2), the layered digest base-context (U3),
contracts-only neighbours, the merge fill penalty (U12) and ADR-0007
fresh-start workers are all **merged but unexercised by real workers**. This
plan's own run is their first live validation; that is intentional and is why
wave 2 was chosen to be almost entirely additive (a new subcommand and a new
skill directory) rather than another rewrite of the execution path.

### The advisory report already carries machine-usable seams

`build_advisory_report` (`orchestrator/grouping/advisory.py:148`) writes
`preview/advisory.json` under `.orchestrator/groupings/<name>/`. Its
`AdvisoryReport` is `{version, plan_path, granularities, cohesion}`; each
`CohesionFinding` (`advisory.py:116`) is
`{kind: disconnected|serial|monolithic, message, task_sets, boundary}`, with
`task_sets` populated for `disconnected` and `boundary` for `serial` — never
both. Measured live on the 36-unit 2026-08-26 plan, the `disconnected` finding
returned `task_sets` of 35 and 1 tasks: a real seam, and a good illustration of
why the operator must be able to override it.

Two gaps for R16: the human rendering (`_print_advisory_report` in
`orchestrator/cli.py`) prints the finding's `message` but **not** a stable index
and **not** the task sets, so there is nothing for `--seam N` to address yet.

### The plan-surgery primitives that exist and the one that does not

- `parse_plan_sections` (`orchestrator/grouping/plan_sections.py:190`) returns
  `PlanSections{preamble, units, digest, flags}`; each `UnitSection` carries
  `unit_id, title, text (verbatim, found in source), summary,
  summary_is_fallback, verification, implements, consumes`.
- `_split_bullets` (`plan_sections.py:93`) collects **any** top-level
  `- **Label**: value` bullet into a dict and ignores labels it does not know.
  Verified by reading: new slots (`Edge cases`, `Non-goals / must-not`) parse
  harmlessly today and need no parser change to survive — unit sections travel
  verbatim into group specs, so deepen's enrichment reaches workers for free.
- `parse_task_map` / `parse_task_map_for_pricing` (`plan_reader.py`) locate the
  fenced map with the module-level `_BLOCK` regex and `yaml.safe_load` it.
  There is **no writer** anywhere in the codebase — no `yaml.dump`, no
  serializer for the map. That is a feature, not a gap: task entries are
  top-level `  - task_id:` list items, so a split can slice them as text and
  keep comments, ordering, and formatting byte-identical. What is missing is a
  helper exposing the fence's span; only the regex exists.
- `compute_partition` calls `compile_base_context` (`pipeline.py:615`), which
  calls `parse_plan_sections` (`base_context.py:41`). So `group --no-spec`
  **already** fails on structural breakage — a missing unit section, a mangled
  heading, a map entry with no matching `### U<N>`. What it cannot catch is
  silent semantic drift: a quietly edited `depends_on`, `size_hints` value, or
  unit id that still parses.

### How verification items reach a run today

`VerificationItem` is `{id, description, required}` (`orchestrator/model.py:32`).
Since wave 1 the items are assembled verbatim from plan Verification bullets
(`assemble_group_specs`, `orchestrator/grouping/assembler.py:170`) with ids
`^g\d+-\d+$`, and `_lint_verification_coverage` (`assembler.py:308`) already
enforces that every plan bullet lands in exactly one group. They are handed to
the coder (`orchestrator/prompts/coder.md:12`, `$verification`) and the reviewer
(`orchestrator/prompts/reviewer.md`, `$verification`), and every one must come
back in `verification_results` (shape at
`orchestrator/prompts/report_contract.md:7`) with status pass | fail | skipped
(enumerated at `orchestrator/prompts/worker_ground_rules.md:72-73`).
`reviewer.md:9-10` instructs the reviewer to "run the tests the spec calls
for" — i.e. **the reviewer infers the command from prose today**, which is the weak-self-verification failure mode the
R19a benchmarks named as dominant. Adding `Run:`/`Pass:` lines is a pure
convention change inside `description`; no schema, assembler, or prompt-contract
change is required.

### CLI conventions

`orchestrator/cli.py` registers flat verb subcommands — `group`, `run`,
`resume`, `groupings`, `status`, `answer`, `retry`, `finish`, `export`,
`calibrate`, `ui` (`cli.py:195–369`). There is no nested sub-subcommand parser
anywhere, so wave 2's commands are flat verbs too. `group` already carries the
zero-LLM preview flags `--dry-run`, `--no-spec`, `--advise`, `--price`
(`cli.py:204–240`), all of which write only under `preview/` and never touch a
persisted `groups.json`.

### Sizes that matter for budgeting

`orchestrator/cli.py` is 106 KB and `tests/test_cli.py` is 86 KB — together
~66k node work, which is why this plan puts the new CLI tests in new files
(`tests/test_plan_edit.py`, `tests/test_plan_split.py`) and never opens
`tests/test_cli.py`.

### Deliberately not fixed here

Running `--advise` on the 36-unit plan returned **identical** metrics for all
three granularity presets (29 groups, same makespan, same modularity, all three
flagged pareto-dominant). Either the dial does not bite on that plan's shape or
a preset is being applied three times. This is a tuning question with only two
or three good plans to test against; per the 2026-08-29 grill it waits for the
R20 eval harness and is recorded in the backlog below rather than guessed at
now.

### Symbols stay empty

Per R9/C4: on this dense codebase populating `symbols` added 103 inferred
precedence edges and degenerated the partition. This plan's map declares no
symbols; `depends_on` and shared-file affinity carry the structure.

## Decisions

- **One shared plan-surgery module is the write-safety mechanism, not a
  convention.** `orchestrator/grouping/plan_edit.py` does verbatim extraction
  and reassembly of unit sections and task-map entries, and exposes
  `verify_map_unchanged(before, after)` for a byte-level guarantee. Both
  `split` and `/orchestrator-deepen` write through it, and it is exposed as
  `orchestrate plan-check` so a skill can verify its own edit. Rejected:
  skill convention plus a `group --no-spec` re-run (catches structural breakage
  but not a silently edited `depends_on`); a guard inside deepen only (split
  needs the same verbatim-extraction primitives, so it would be duplicated).
- **Seams are addressable from the report *and* overridable, and the plan skill
  drives it too.** `split --seam N` consumes `preview/advisory.json`;
  `--tasks u1,u2 --tasks u3,u4` overrides when the operator disagrees — as they
  would with the measured 35-vs-1 seam; and `/orchestrator-plan` reads the
  advisory, asks, and invokes `split` with the chosen assignment. Per the
  2026-08-29 grill: a smart CLI the skill can steer, on the reasoning that
  every plan is a different plan and the eval harness has not yet told us what
  to hard-code. Rejected: `--tasks` only (re-types what `--advise` computed),
  skill-driven only (split unusable standalone or scripted).
- **Flat CLI verbs: `split` and `plan-check`.** Matches every existing
  subcommand; the codebase has no nested sub-parser pattern to follow.
- **`split` is non-destructive.** It writes N new documents beside the original
  and leaves the original in place, printing what it wrote and what to archive.
  Output naming keeps the plan-file convention: `<stem>-part<N>-plan.md` from
  `<stem>-plan.md`. Rejected: rewriting the original in place (unrecoverable if
  the seam was wrong), regenerating filenames with a fresh `NNN` (breaks the
  traceable link back to the source plan).
- **Deepen emits `Run:` + `Pass:` verification lines, explorer-grounded.**
  Grounding means two checks the explorer can actually make: the runner idiom is
  one this repo really uses, and every path in the command appears in that
  unit's declared `files`. A command failing either check degrades to a
  `Pass:`-only condition. `Run:` must be the narrowest command that proves the
  item — never a bare full-suite invocation, which the preflight baseline gate
  already covers. Rejected: `Pass:`-only v1 (leaves the reviewer inferring
  commands, the dominant measured failure mode); ungrounded commands (a wrong
  command burns a reviewer round every time it fires).
- **No schema change for verification.** `Run:`/`Pass:` are conventional lines
  inside the existing `description`; `VerificationItem`, the assembler, and the
  prompt contract are untouched. Rejected: structured command/condition fields
  (a migration across every existing plan and `groups.json` for no gain the
  convention does not already deliver).
- **The per-group question cap dominates the plan-global cap.** 3–5 questions
  per group, always offered with candidate answers, even when a large plan
  therefore exceeds ~10 questions overall. Per the 2026-08-29 grill: every group
  gets clarified rather than losing its questions to a cross-group ranking.
  Rejected: global cap dominant (a low-ranked group is silently skipped and
  ships unclarified), global-with-a-floor (the floor eats the budget on big
  plans anyway).
- **Edge cases are written only where they fire.** Taxonomy-keyed one-liners,
  no `N/A — <why>` filler. The explorer still walks all ten categories
  internally, so coverage lives in the process, not in ten lines per unit that
  every worker on that unit pays to read. Rejected: mandatory `N/A` entries
  (~10 extra lines per unit against the token-optimality preference); a
  high-risk-only subset (the always/never split becomes its own judgment call
  to maintain).
- **Granularity-preset convergence is deferred, not guessed.** Recorded in the
  backlog for the R20 harness.

## Unit summaries
- U1 (plan-edit — verbatim plan surgery and the `plan-check` guard) Summary: A shared `plan_edit` module that extracts and reassembles unit
  sections and task-map entries byte-verbatim and refuses any rewrite that
  perturbs the map or unit ids, exposed as `orchestrate plan-check`.
- U2 (plan-split — `orchestrate split`, seam-addressable and overridable) Summary: `orchestrate split` partitions a plan into N documents by moving
  unit sections and task-map entries verbatim, taking its assignment either
  from a numbered advisory seam or from explicit `--tasks` groups.
- U3 (deepen-skill — `/orchestrator-deepen`, explorer-grounded and capped) Summary: A standalone interactive skill that explores the codebase
  read-only per group, grills the human with 3–5 EVPI-ranked questions per
  group, and writes the answers back into the plan as per-unit edge cases,
  non-goals, and `Run:`/`Pass:` verification items.
- U4 (planning-contract — the plan skill and the contracts learn about wave 2) Summary: `/orchestrator-plan`'s advisory phase can now invoke `split`,
  and the task-map and grouping contracts document the new unit slots, the
  `Run:`/`Pass:` convention, and the two new commands.

## Implements / Consumes registry
- U1 implements: plan-edit, plan-check-cli
- U2 implements: split-cli, advisory-seams
- U2 consumes: plan-edit
- U3 implements: deepen-skill
- U3 consumes: plan-edit
- U4 consumes: split-cli, deepen-skill, advisory-seams
