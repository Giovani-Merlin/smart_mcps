---
date: 2026-07-15
topic: multiagent-orchestrator
phase: B (execution engine, U5–U8)
plan: docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md
branch: feat/multiagent-orchestrator
---

# Phase B handoff — multi-agent orchestrator execution engine

For the session executing Phase B. Phase A (grouping engine, U1–U4) is complete,
reviewed, and committed on `feat/multiagent-orchestrator`. Nothing is pushed; no PR
exists. The plan is the decision artifact — this handoff adds only session-learned
context the plan does not carry.

## How to start

```
/compound-engineering:ce-work Execute Phase B of the plan docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md
```

- Stay on branch `feat/multiagent-orchestrator` (do not create a new one; Phase C
  ships the whole feature from here).
- Read the plan's **Key Technical Decisions**, **U5–U8**, and the two research
  docs it pins as hard constraints before writing code:
  `docs/research/infinity-skills-analysis.md` §6 (every recommendation is a hard
  constraint on U5) and `docs/research/cocoder-analysis.md` §5/§8 (scheduler and
  watchdog designs).

## Start with the U5 spike — before any U5 implementation

The plan's main mechanical unknown. Answer with the real CLI (spend a few tokens;
everything after this is stub-tested):

1. Does `claude -p --fork-session --resume <base-id>` work in print mode, and does
   it honor `--session-id <uuid>` for the fork? (If it assigns its own ID, that is
   fine — record observed IDs in the manifest; the join contract holds.)
2. What are the exact usage field names in the `--output-format json` envelope?
   (The circuit breaker reads cumulative usage from them.)
3. Does `--json-schema` combine with `--resume`? (Mapper/speccer degrade to
   prompt-enforced JSON + validation retries if not — only matters for U7's
   speccer re-invocation on rewrite.)

If print-mode forking is unusable: fall back to fresh sessions with an identical
compiled head (the base-context document already compiles byte-stably —
`orchestrator/grouping/base_context.py`) and **record the deviation in
`docs/research/design-deviations.md`** (now git-tracked; R23 keeps it current).
Fork calls are always serialized behind an orchestrator lock — the spike does not
need to test concurrent forking.

## What Phase A left you

Package layout (all under `orchestrator/`):

- `model.py` — the contracts Phase B consumes: `Group`, `GroupingResult`,
  `RunManifest`, `GroupManifestEntry`, `SessionEntry` (role/generation/
  retirement_reason), `CoderReport`, `ReviewerVerdict`, `Surprise`,
  `ReviewIntensity`. Session display-name convention lives in a comment on
  `SessionEntry.name`: `<run_id>-<group_id>-<role>-g<generation>`.
- `config.py` — `BreakerConfig` (120k context tokens, 3 rounds/generation,
  3 generations) and `ExecutionConfig` (concurrency 3, `sequential` flag for R25,
  `permission_mode`) are **already defined and tested but nothing reads them yet**
  — U6/U7 are their intended consumers. Same for `VerificationItem.required` and
  the `fan_in`/`fan_out` metadata (deliberately kept for Phase B).
- `grouping/pipeline.py` `run_grouping()` returns `(GroupingResult, base_context text)`; `serialize_grouping()` is the canonical groups.json serializer. The CLI
  `smart-mcps-orchestrate group <plan>` (no `--dry-run`) writes
  `.orchestrator/groups.json` + `.orchestrator/base-context.md` — U6's `run`
  command builds on these artifacts.
- `grouping/partition.py` `build_group_dag(graph, partition)` gives
  `{upstream_gid: {downstream_gid}}` — but note groups.json already carries
  per-group `dependencies` (upstream group-id strings), which is what the
  scheduler should read.
- `grouping/llm.py` `call_llm_json` — the retry-nudge seam; U7's speccer
  re-invocation goes through `grouping/speccer.py::write_specs` unchanged.
- `prompts/` — mapper.md and speccer.md exist; U5 adds identity block, coder,
  reviewer, and handoff templates here (`string.Template` `$placeholders`, loaded
  via `orchestrator.prompts.load_template`). Templates ship as package data
  because `prompts/` has an `__init__.py` — keep that.

Tests: 113 passing, zero LLM/CLI tokens (`uv run pytest tests/ -q`). Phase B keeps
this property via `tests/fake_claude.py` (to be written in U5) — a scripted stub
`claude` executable speaking the real CLI surface the spike verifies.

## Unit order and the non-obvious constraints

U5 (sessions/worktrees/manifest) → U6 (scheduler) → U7 (review loop) → U8 (merges).
U5's full spec is in the plan; the analyzer-contract details that are easy to miss:

- Worktrees at `<repo>/.worktrees/<group_id>-<slug>/` — the repo directory name
  must stay a **path substring** or infinity-skills' allowlist silently drops the
  sessions.
- A group's worktree branches from the **current tip of the run's integration
  branch** at ready→running transition, never the original launch ref.
- All run artifacts under `.orchestrator/runs/<run_id>/` in the target repo —
  never under `~/.claude/projects/`.
- Round messages ferry pointers, not payloads: reports/verdicts persist under
  `.orchestrator/runs/<run_id>/groups/<group_id>/`; the reviewer reads the diff
  from git and the report from disk.
- First worker prompt: identity block `<run-manifest run_id=... group_id=... group_name=...><summary>...</summary></run-manifest>` then `<spec>...</spec>`;
  summary ≤120 chars is already enforced by `Group.summary`.
- Preflight: verify the installed CLI supports `-p`, `--output-format json`,
  `--resume`, `--fork-session`, `--session-id`, `-n`; fail with a versioned
  message otherwise.

## Phase A gotchas that will bite Phase B

- **The PostToolUse format hook removes imports that are momentarily unused.** If
  you add an import in one edit and its usage in a later edit, the hook may strip
  the import in between. Batch import+usage in one Write/Edit, or re-check.
- **codegraph CLI quirks** (handled in `grouping/graphing.py`, pattern to reuse if
  U5+ shells other CLIs): exit 0 + plain text `ℹ Symbol "X" not found` for
  missing symbols; ANSI escapes in piped output; default `-l 20` truncation.
- **`docs/research/perplexity/` and `docs/research/manual/` are still gitignored**
  (auto-saved query dumps); the curated research docs are tracked as of this
  session.
- The suite must stay token-free (R24): if a test needs `claude`, it needs
  `fake_claude.py`, not the real binary. A single manual live smoke run happens at
  U5's end (create base session, fork, round-trip a report) and one more at U10.
- Repo conventions: Python 3.12, ruff line 100, pytest + pytest-asyncio (U6's
  asyncio scheduler tests will want `asyncio_mode` config or explicit markers —
  check `tests/test_codegraph_server.py` for the existing async test pattern).
- Review-tier note: Phase A ran the harness-native tiered review inline; the plan
  is to run the full deep review once, at the end-of-plan PR, not per phase.

## Definition of done for Phase B

Every U5–U8 test scenario in the plan green against `fake_claude.py` with zero
live calls; the U5 manual smoke run done and its findings (fork semantics, usage
field names) recorded in `docs/research/design-deviations.md`; scheduler state
file inspectable mid-run; merge tests on scripted git fixture repos passing.
Phase C (U9 CLI surface + U10 E2E harness/docs) remains after that.
