---
title: Human-facing run reports generated from run artifacts
type: feat
date: 2026-09-02
origin: direct
---

# Human-facing run reports generated from run artifacts

## Objective

After an orchestrator run, the owner can decide two things without opening
the diff or the transcripts: **approve the merge** and **write the next
plan**. Today that decision rests on `.orchestrator/notes-<run_id>.md`, a
free-form LLM write-up that runs 29–467 lines and is rarely read.

This plan ships four trial formats plus the shared machinery under them, all
rendered from the run directory and git with zero LLM calls, and one
LLM-authored one-pager gated by a validator. Every format is generated for
two finished fixture runs so the owner can compare them side by side and
pick:

| trial | format                                                   | LLM tokens |
| ----- | -------------------------------------------------------- | ---------- |
| A     | run changelog entry (fragments per group, `RUNLOG.md`)   | 0          |
| B     | single-file HTML report with six visualizations          | 0          |
| C     | PR body as the record, postmortem-lite when troubled     | 0          |
| D     | 300-word one-pager, written by the run driver, validated | capped     |

Success criteria (the owner's, from the grill): could I approve the merge
from this alone; could I write the next plan from this alone; every claim
links to evidence; visuals are computed, never drawn by an LLM.

Placement (decided): the runner emits facts, the run-driver session fills
the one capped narrative, `.orchestrator/config.toml` `[docs]` picks
formats. The plan and deepen skills are untouched. The record lives
committed under `docs/runs/<run_id>/` on the integration branch (→ ADR 0008).

## What we already know (resolved context)

**Where the bad docs come from.** `skills/orchestrator-run/SKILL.md` Phase 4
step 4 ("Write the summary into `.orchestrator/notes-<run_id>.md`: per-group
outcome, every escalation …") gives the driver session no template, no cap,
and no evidence rule. That step is what this plan replaces.

**Every fact already exists on disk.** For a run `<id>`, `RunPaths(repo_root, run_id)` (`orchestrator/execution/manifest.py:162`) resolves
`.orchestrator/runs/<id>/`, which holds:

- `manifest.json` — `plan_path`, `launch_branch`, `groups{gid: {group_name, summary, sessions[{role, generation, model, started_at, ended_at, retirement_reason, total_*_tokens, transcript_path}]}}`.
- `state.json` — `groups{gid: {state, failure}}` (`RunState`).
- `groups.json` — `{plan_path, groups[{id, name, summary, spec, tasks[], files[], dependencies[], verification[{id, description}], intensity, difficulty, estimated_tokens}], flags[]}`. Verification item ids are
  `<gid>-<n>`.
- `groups/<gid>/report-<gid>-r<n>.json` — `{status, summary, verification_results[{item_id, status, notes}], surprises[]}`; the coder's
  `notes` name the tests that proved each item.
- `groups/<gid>/verdict-<gid>-r<n>.json` — `{status, required_changes[], surprises[], notes}`.
- `groups/<gid>/preflight-junit.xml` + `preflight-check.log` — the merge
  gate's real test results for that group; `preflight-baseline-junit.xml` and
  `preflight-baseline.json` (`commit_sha`, `exit_code`, `captured`) at the run
  root are the baseline. `orchestrator/execution/preflight.py` already
  parses junit (`_parse_junit_results`).
- `logs/run.log` — timestamped events, including
  `finish <id>: pushed orchestrator/run-<id> to origin at <sha>` and
  `finish <id>: opened PR <url>`.
- `escalations/` — request/response files (absent when none).

`orchestrator/execution/export.py:329` `build_export(paths, project=…)`
already composes most of this into `RunExport` (`ExportGroup` with
`final_state`, `failure`, `stale_failure`, `sessions[ExportSession]`,
`artifacts[ExportArtifact{kind, round, status, surprises, required_changes, path}]`, `escalations[ExportEscalation{prompt, action, answer, created_at}]`). It imports `orchestrator.observatory.runs.build_snapshot`
lazily (fastapi). Reuse it; do not re-read the JSON by hand.

**Git range of a run.** Base = `preflight-baseline.json` `commit_sha`. Tip =
the sha in the `finish … pushed … at <sha>` log line, else `git rev-parse orchestrator/run-<id>` (`integration_branch()` in
`orchestrator/execution/worktrees.py:200`) when the branch still exists,
else unavailable. Per-group attribution is exact: the integration branch's
first-parent history carries one merge commit per group, subject
`merge(<run_id>): <gid> <slug>`, so `git diff --numstat <merge>^1 <merge>`
is that group's footprint. Both fixture runs' base and tip commits are
reachable from `main` (verified).

**Plan text per unit.** `orchestrator/grouping/plan_sections.py`
`parse_plan_sections(text)` returns `PlanSections.units{unit_id: UnitSection(unit_id, title, text, summary, verification[], implements, consumes)}`; `unit_key_for_task("u3-…") == "u3"`. The plan's frontmatter
`origin:` names the brainstorm doc; R-IDs appear in unit prose as `R<n>`
(e.g. `docs/brainstorms/2026-08-28-grouper-speccer-flow-requirements.md`
lists `- R1.` … `- R36.`). No existing parser reads the frontmatter; a
six-line `---` block scan is enough.

**PR body today.** `orchestrator/execution/finish.py:241` `_render_pr_body`
emits one bullet per group (state, verdict, session count, summary) and an
unmerged list; `_open_pr` passes it via `gh pr create --body-file -`.
`tests/test_finish.py::test_pr_body_lists_groups_state_summary_sessions_and_unmerged`
pins the current shape and must be updated, not deleted. `finish_run` runs
after every group is merged, before push; it is the right place to generate
and commit the report onto the integration branch.

**CLI and config conventions.** Subcommands are `argparse` subparsers in
`orchestrator/cli.py` (`export_cmd` at line 429, dispatch at line 492,
`_cmd_export` at 2825 — lazy-imports `orchestrator.execution.export`).
Config sections are pydantic models on `OrchestratorConfig`
(`orchestrator/config.py:598`), loaded by `load_config` from
`.orchestrator/config.toml`; unknown keys are silently ignored by pydantic,
so a new `[docs]` block needs no migration. Plugin version lives in
`.claude-plugin/plugin.json` (`0.14.1`); commits name the bump.

**Dependencies.** `pyproject.toml` has no templating library. Jinja2 is
added for the HTML trial (a real wheel, no build step). Towncrier is **not**
added: its fragment model is copied in ~80 lines because the orchestrator
runs against foreign repos that may not be Python and must not depend on the
target repo's release tooling. Mermaid renders natively on GitHub for the
markdown trials; the HTML trial loads `mermaid@11` from jsdelivr and falls
back to the source in a `<pre>` when offline.

**codegraph for sequences.** The `codegraph` CLI on PATH offers `callees <symbol> --json --limit N` and `callers`; there is no CLI `trace` (that only
exists on the MCP proxy in `codegraph_mcp/server.py`). The how-to-use
diagrams therefore walk `callees` to depth 2 from each new entry point and
skip with a note when `codegraph` or `.codegraph/` is absent.

**Fixture runs, visible to workers.** Workers see only committed files, so
the two fixture runs were copied to `tests/fixtures/runs/r20260829-162627/`
(3 groups, plan `docs/plans/2026-08-29-001-feat-plan-split-and-deepen-plan.md`,
base `5eca4f14`, tip `a76fec68`, no surprises) and
`tests/fixtures/runs/r20260828-220035/` (11 groups, plan
`docs/plans/2026-08-28-001-feat-deterministic-grouper-advisory-plan.md`,
base `a2098a08`, tip `93dadc02`, 4 surprises → the postmortem-lite gate
fires). **Precondition, outside every unit:** the run driver commits
`tests/fixtures/runs/` (4.6 MB) together with this plan and ADR 0008 on
`main` before launching; no unit lists the fixtures under `files`, and the
preflight's clean-tree gate would refuse to launch with them untracked. Their `manifest.json`
`transcript_path`s point at home-directory files that do not exist in a
worktree; `build_export` already marks these `transcript_missing` and the
report must tolerate it. `RunPaths` has no run-dir override today; U1 adds
one so a fixture directory can stand in for `.orchestrator/runs/<id>/`.

**Not in scope.** Audio (NotebookLM) briefs, Observatory UI changes, cost
and change-footprint charts (visuals 5 and 6 from the grill), committing
anything to `main` from the orchestrator, and any change to
`/orchestrator-plan` or `/orchestrator-deepen`.

## Decisions

- **Facts are computed, never written.** A `RunFacts` model is the single
  source for every format; "tests pass" comes from junit counts or reads
  "no tests ran". Rejected: improving the driver's prompt (the research and
  the 13 existing notes files show free narrative is the failure mode).
  (→ ADR 0008)
- **The LLM slot is a validated one-pager, not a section of the record.**
  The run driver writes `one-pager.md` from a scaffold; `report --validate`
  rejects it on headings, bullet counts, word count, missing pointers, or
  banned phrases. Rejected: a documentation unit inside the plan (cold
  worker, serial tail, never saw the escalations); an LLM call inside the
  runner (no memory of the run).
- **Placement: `finish` generates and commits; config selects.** `[docs] formats` empty by default (opt-in). Rejected: asking at plan time (the plan
  cannot know what the run will need documenting); a `run --docs` flag
  (config already covers it; add later if wanted).
- **Record lives at `docs/runs/<run_id>/` on the integration branch.**
  Survives restarts, ships in the PR, links resolve. Rejected: gitignored
  `.orchestrator/` (vanishes with the checkout); PR body only (squashes lose
  it).
- **Fragment model without the towncrier dependency; Jinja2 added.** See
  resolved context. Rejected: git-cliff / release-please (commit-driven,
  would need synthetic commits).
- **Per-group attribution via first-parent merge commits.** Exact and
  already there. Rejected: planned-files ∩ touched-files heuristic (kept
  only as the fallback when the range is unavailable).
- **Six visualizations, all mermaid or plain HTML tables:** evidence
  matrix, requirement traceability, architecture delta, how-to-use
  sequences, run timeline, plan-to-outcome map. Rejected for now: cost
  and footprint charts (owner's choice), any JS charting library.

## Units

### U1. Run facts model, `report` command, `[docs]` config — one deterministic source for every format

- **Summary**: `RunFacts` (built from `build_export`, `groups.json`, the plan's unit sections, junit, and the run's git range) plus a `smart-mcps-orchestrate report <run> --format facts` subcommand and a `[docs]` config block.
- **Goal**: `orchestrator/report/facts.py` exposes `build_facts(repo_root, run_id, *, run_dir=None) -> RunFacts` and the CLI writes `facts.json`. `RunFacts` carries: run meta (`run_id`, `plan_path`, `plan_title`, `plan_objective` — the `## Objective` section text —, `origin`, `created_at`, `finished_at`, `pr_url`); `git_range` (`base_sha`, `tip_sha`, `available: bool`); `changed_files[{path, added, deleted, group_id | null}]` attributed via first-parent `merge(<run_id>): <gid> …` commits, with `group_id: null` and the planned-files fallback when the range is unavailable; `groups[GroupFacts{id, name, state, verdict_status, failure, stale_failure, tasks, planned_files, touched_files, sessions[{role, generation, model, started_at, ended_at, retirement_reason, tokens}], escalations[], surprises[], required_changes[], tests{ran: bool, total, failures, errors, junit_path}}]`; `units[UnitFacts{unit_id, task_id, group_id, title, summary, goal, verification[{item_id, description, status: pass|fail|unverified, evidence}], landed: bool}]` where `status` comes from the **latest** round's `verification_results` and `evidence` is its `notes`; `rids[{rid, units, landed}]` from `R<n>` mentions in unit prose against the origin doc's `- R<n>.` lines; `timeline[{at, kind, group_id, label}]` (session start/end, retirement, escalation, merge); `adr_delta[{path, change}]` from `git diff --name-status base tip -- docs/adr/`; `trouble: bool` (any non-stale failure, retirement, escalation, surprise, or required change). `RunPaths.__init__` gains an optional `run_dir` override. `DocsConfig(formats: list[str] = [], out_dir: str = "docs/runs")` is added to `OrchestratorConfig` as `docs`. The CLI also accepts `--run-dir` (fixture stand-in), `--out DIR` (default `<repo_root>/<docs.out_dir>/<run_id>/`), and `--format`, which this unit registers with the single value `facts`; later units add theirs.
- **Files**: `orchestrator/report/__init__.py` *(new, small)*, `orchestrator/report/facts.py` *(new, large)*, `orchestrator/execution/manifest.py`, `orchestrator/config.py`, `orchestrator/cli.py`, `tests/test_report_facts.py` *(new, large)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `report-facts`
- **Verification**:
  - Run: `uv run smart-mcps-orchestrate report r20260828-220035 --run-dir tests/fixtures/runs/r20260828-220035 --format facts --out /tmp/facts-11`. Pass: exits 0 and `/tmp/facts-11/facts.json` has 11 groups, `git_range.available == true` with `base_sha` starting `a2098a08` and `tip_sha` starting `93dadc02`, `trouble == true`, and at least four surprises across groups.
  - Run: the same against `tests/fixtures/runs/r20260829-162627`. Pass: 3 groups, 3 units, every unit `landed == true`, every verification item `status == "pass"` with non-empty `evidence`, and `changed_files` totals match `git diff --numstat 5eca4f14 a76fec68` (13 files, 1874 insertions, 10 deletions).
  - Run: `uv run pytest tests/test_report_facts.py -q`. Pass: a synthetic run whose report has a `fail` item and a verdict with a `required_change` yields `trouble == true` and that unit `landed == false`; a run with no baseline file and no branch yields `git_range.available == false` and files attributed by the planned-files fallback; a group with no `preflight-junit.xml` yields `tests.ran == false`.
  - Run: `uv run smart-mcps-orchestrate report r20260829-162627 --run-dir tests/fixtures/runs/r20260829-162627 --format facts --out /tmp/f && python -c "import json;d=json.load(open('/tmp/f/facts.json'));print(d['rids'][:3])"`. Pass: R-IDs `R16` and `R17` appear with their claiming units, resolved against `docs/brainstorms/2026-08-28-grouper-speccer-flow-requirements.md`.
  - Run: `uv run python -c "from orchestrator.config import load_config; from pathlib import Path; print(load_config(Path('.orchestrator/config.toml')).docs)"`. Pass: prints `formats=[] out_dir='docs/runs'` (the existing config has no `[docs]` block and keeps loading).
- **Edge cases**: —
- **Non-goals / must-not**: must not read transcripts; must not call any LLM; must not write outside `--out`.

### U2. Mermaid diagrams — timeline, plan-to-outcome map, architecture delta, how-to-use sequences

- **Summary**: `orchestrator/report/diagrams.py` renders four mermaid sources from `RunFacts` and git: a gantt run timeline, a plan-to-outcome flowchart, a Python import-graph delta between the run's base and tip, and depth-2 `codegraph callees` sequence diagrams for new entry points.
- **Goal**: Pure functions returning mermaid text: `timeline_gantt(facts)` (one section per group, coder/reviewer generations as bars from `started_at` to `ended_at`, escalations and retirements as milestones); `plan_outcome_flowchart(facts)` (unit → group → final state, class `ok|fail|resolved`, reviewer verdict on the edge label); `architecture_delta(facts, repo_root)` (for every `.py` in `changed_files`, parse `import`/`from … import` with `ast` from `git show <base>:<path>` and `git show <tip>:<path>`, keep intra-repo module edges, render a flowchart with classes `added`, `removed`, `kept`; when no Python file changed or the range is unavailable, return a one-line `%% note` explaining why); `howto_sequences(facts, repo_root)` (entry points = subcommand names added by the diff via `add_parser("<name>"` and public functions defined in files whose status is added; for each, `codegraph callees <symbol> --json --limit 20` walked to depth 2, rendered as `sequenceDiagram`; returns `[]` with a note when `codegraph` is not on PATH or `.codegraph/` is missing). `render_all(facts, repo_root) -> Diagrams` bundles them and is what U3/U4 consume.
- **Files**: `orchestrator/report/diagrams.py` *(new, large)*, `tests/test_report_diagrams.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: implements `report-diagrams`; consumes `report-facts`
- **Verification**:
  - Run: `uv run python -c "from orchestrator.report.facts import build_facts; from orchestrator.report.diagrams import architecture_delta; from pathlib import Path; f=build_facts(Path('.'),'r20260829-162627',run_dir=Path('tests/fixtures/runs/r20260829-162627')); print(architecture_delta(f, Path('.')))"`. Pass: output starts with `flowchart` and names `orchestrator.grouping.plan_edit` as an added node with an edge to `orchestrator.grouping.plan_reader` (that module was created by the run and imports the reader).
  - Run: the same for `timeline_gantt` and `plan_outcome_flowchart` on the 11-group fixture. Pass: the gantt has 11 sections and at least 22 task lines (coder and reviewer per group); the flowchart has 11 group nodes, every one classed `ok`.
  - Run: `uv run pytest tests/test_report_diagrams.py -q`. Pass: a synthetic facts object with `git_range.available == false` yields a single `%% note` line from `architecture_delta`; with `PATH` scrubbed of `codegraph`, `howto_sequences` returns `[]` plus a note and does not raise; every renderer's output parses under the mermaid grammar's structural minimum (first line is `gantt`, `flowchart LR`, or `sequenceDiagram`).
  - Run: `uv run python -c "…howto_sequences(f, Path('.'))"` on the 3-group fixture with the codegraph index present. Pass: at least one `sequenceDiagram` whose participants include `plan-check` or `split` (both subcommands were added by that run).
- **Edge cases**: —
- **Non-goals / must-not**: no JS charting libraries; no LLM; never fail the report because codegraph is absent.

### U3. Markdown trials — run changelog entry, PR body as the record, postmortem-lite

- **Summary**: `orchestrator/report/markdown.py` renders trial A (`CHANGELOG-entry.md` from per-group fragments plus an idempotent section in `docs/RUNLOG.md`) and trial C (a fixed-template PR body that `finish` now uses, with a postmortem-lite section when `facts.trouble`).
- **Goal**: `render_fragments(facts) -> dict[gid, str]` writes one markdown fragment per group (state, ≤20-word summary trimmed from the coder report, verification pass/fail counts, surprises, escalation actions, tokens, elapsed), and `render_changelog_entry(facts, diagrams) -> str` compiles them under `## <date> — <run_id> — <plan_title>` with: a three-line TL;DR (outcome: `N/M groups completed, K/U units landed`; scope: files and +/− lines; cost: total tokens by model), per-unit blocks (title, summary, a verification table `item | status | evidence`, verdict, surprises), the postmortem-lite section when `trouble` (Impact: units not landed and R-IDs unmet; Timeline: ≤6 computed events; Root-cause candidates: `failure`, `retirement_reason`, `required_changes` verbatim with their artifact paths; Follow-ups: open `required_changes` and `fail` items), the gantt and plan-outcome diagrams, and the ADR delta. Every bullet ends with a pointer in parentheses (a repo path, `<gid>`, `<unit_id>`, an item id, a sha, or `R<n>`). `update_runlog(runlog_path, run_id, entry)` replaces the text between `<!-- run:<id> -->` and `<!-- /run:<id> -->` markers or appends. `render_pr_body(facts) -> str` has exactly the headings `## Motivation`, `## Changes`, `## Risks`, `## Testing`, `## Handoff` (+ `## Postmortem` when troubled): Motivation is the plan objective's first paragraph plus the R-ID list; Changes is one bullet per unit tagged `[gid]` when units ≤ 7, else one per group; Risks lists surprises, required changes, unmerged groups, or `none recorded`; Testing lists per-group gate counts from junit or `no tests ran`; Handoff links `docs/runs/<run_id>/`. `finish._render_pr_body` delegates to it; the existing test is rewritten to assert the new headings. The `report` CLI gains `--format changelog|pr-body` (writing `CHANGELOG-entry.md`, `pr-body.md`, and updating `docs/RUNLOG.md` under `--out`'s repo).
- **Files**: `orchestrator/report/markdown.py` *(new, large)*, `orchestrator/execution/finish.py`, `orchestrator/cli.py`, `tests/test_report_markdown.py` *(new, large)*, `tests/test_finish.py`
- **Symbols**: —
- **Depends-on**: U1, U2
- **Slice**: —
- **Implements / Consumes**: implements `report-markdown`; consumes `report-facts`, `report-diagrams`
- **Verification**:
  - Run: `uv run smart-mcps-orchestrate report r20260828-220035 --run-dir tests/fixtures/runs/r20260828-220035 --format changelog --out docs/runs/r20260828-220035`. Pass: `docs/runs/r20260828-220035/CHANGELOG-entry.md` exists, contains a `## Postmortem` section listing the four recorded surprises verbatim with their `report-*.json` paths, and `grep -c '^- ' … ` bullets all end with `)`; `docs/RUNLOG.md` holds exactly one `<!-- run:r20260828-220035 -->` block after running the command twice.
  - Run: the same for `r20260829-162627`. Pass: the entry has no `## Postmortem` section, three per-unit blocks whose verification tables show every item `pass` with the test names from the coder notes, and a `flowchart` block with three `ok` nodes.
  - Run: `uv run smart-mcps-orchestrate report r20260829-162627 --run-dir tests/fixtures/runs/r20260829-162627 --format pr-body --out docs/runs/r20260829-162627 && wc -w docs/runs/r20260829-162627/pr-body.md`. Pass: the five headings appear in order, `## Testing` lists three groups with pytest counts parsed from their `preflight-junit.xml`, and the body is under 600 words.
  - Run: `uv run pytest tests/test_finish.py tests/test_report_markdown.py -q`. Pass: `test_pr_body_*` asserts the new headings and that `gh pr create` receives the rendered body; a synthetic troubled run renders a `## Postmortem` in both the entry and the PR body, and a run with a stale failure renders none.
- **Edge cases**: —
- **Non-goals / must-not**: never prose beyond the fields above; no LLM; `update_runlog` never rewrites other runs' blocks.

### U4. HTML trial — single-file Jinja2 report with the six visualizations

- **Summary**: `orchestrator/report/html.py` renders trial B: one self-contained `report.html` from a Jinja2 template with per-unit evidence cards, an evidence matrix, requirement traceability, the architecture delta, how-to-use sequences, the timeline, and the plan-to-outcome map.
- **Goal**: Add `jinja2>=3.1` to `pyproject.toml` dependencies. `render_html(facts, diagrams, one_pager: str | None) -> str` fills `orchestrator/report/templates/report.html.j2`: header (plan title, outcome line, git range with short shas, PR link, trouble badge); the one-pager embedded first when present; **evidence matrix** — rows are units, columns are their verification items, cells `pass|fail|unverified` with the evidence text on hover and in a `<details>`; **traceability** — table of R-IDs → units → landed; per-unit cards (summary, goal, planned vs touched files, verdict, surprises, required changes); **architecture delta**, **sequences**, **timeline**, **plan-outcome** as `<pre class="mermaid">` blocks with `mermaid@11` loaded from `https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js` and the raw source visible if the script fails to load; group cost table. Inline CSS only, light and dark via `prefers-color-scheme`, no other external assets. The `report` CLI gains `--format html` and a `--format all` that runs every registered format.
- **Files**: `orchestrator/report/html.py` *(new, medium)*, `orchestrator/report/templates/report.html.j2` *(new, large)*, `orchestrator/cli.py`, `pyproject.toml`, `tests/test_report_html.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U1, U2
- **Slice**: —
- **Implements / Consumes**: implements `report-html`; consumes `report-facts`, `report-diagrams`
- **Verification**:
  - Run: `uv sync && uv run smart-mcps-orchestrate report r20260828-220035 --run-dir tests/fixtures/runs/r20260828-220035 --format html --out docs/runs/r20260828-220035 && ls -la docs/runs/r20260828-220035/report.html`. Pass: the file exists, is under 2 MB, and `grep -c 'class="mermaid"'` reports at least 4 blocks; the evidence matrix has 11 rows.
  - Run: the same for `r20260829-162627`. Pass: the matrix has 3 rows, every cell `pass`, the traceability table lists R16 and R17 as landed, and the architecture-delta block names `plan_edit`.
  - Run: `uv run pytest tests/test_report_html.py -q`. Pass: rendering a synthetic facts object with one `fail` item marks that cell `fail` and the header shows the trouble badge; rendering with `one_pager=None` omits the narrative section; the template raises on an undefined variable (`StrictUndefined`) rather than rendering blanks.
  - Run: open `docs/runs/r20260828-220035/report.html` in a browser via `python -m http.server` and check the console. Pass: the six mermaid blocks render as diagrams with no console error; with network disabled the same page shows the mermaid source in the `<pre>` blocks and no blank areas.
- **Edge cases**: —
- **Non-goals / must-not**: no React, no Observatory coupling, no LLM, nothing fetched but the single mermaid script.

### U5. One-pager contract and validator, `finish` generates and commits the report

- **Summary**: Trial D's scaffold and validator (`report --scaffold one-pager` / `report --validate one-pager.md`) and `finish_run` generating, validating, and committing `docs/runs/<run_id>/` on the integration branch per `[docs] formats` before it pushes.
- **Goal**: `orchestrator/report/onepager.py` provides `scaffold(facts) -> str` (the fixed skeleton: `# <plan_title> — <run_id>`, `## TL;DR` with three placeholder bullets, `## Problems found`, `## Next steps`, and an HTML comment listing every valid pointer: repo paths from `changed_files`, `docs/runs/<id>/…` files, group ids, unit ids, verification item ids, `R<n>`, short shas) and `validate(text, facts) -> list[str]` returning one violation per rule broken: headings exactly as scaffolded and in order; `## TL;DR` has exactly 3 bullets; `## Problems found` and `## Next steps` have 1–5 bullets each; every bullet ends with `(<pointer>)` where the pointer is in the valid set; ≤300 words excluding pointers; none of the banned phrases (`It is important to note`, `Overall`, `In summary`, `leverage`, `robust`, `seamless`); no modal verbs (`should`, `could`, `might`, `may`) in Problems found. The CLI gains `--scaffold one-pager` (writes `one-pager.md` skeleton under `--out`) and `--validate PATH` (prints violations, exit 1 when any). `finish_run` — when `load_config(...).docs.formats` is non-empty — runs the configured formats with `--out <integration worktree>/docs/runs/<run_id>/`, validates `one-pager.md` if present (a failing one-pager aborts `finish` with the violations; an absent one is fine), commits `docs(run): report for <run_id>` on the integration branch, and only then pushes; the PR body is `render_pr_body`.
- **Files**: `orchestrator/report/onepager.py` *(new, medium)*, `orchestrator/execution/finish.py`, `orchestrator/cli.py`, `tests/test_report_onepager.py` *(new, medium)*, `tests/test_finish.py`, `tests/test_cli.py`
- **Symbols**: —
- **Depends-on**: U1, U3, U4
- **Slice**: —
- **Implements / Consumes**: implements `report-onepager`; consumes `report-facts`, `report-markdown`, `report-html`
- **Verification**:
  - Run: `uv run smart-mcps-orchestrate report r20260829-162627 --run-dir tests/fixtures/runs/r20260829-162627 --scaffold one-pager --out /tmp/op && uv run smart-mcps-orchestrate report r20260829-162627 --run-dir tests/fixtures/runs/r20260829-162627 --validate /tmp/op/one-pager.md; echo exit=$?`. Pass: the untouched scaffold fails validation (placeholder bullets carry no pointer) with exit 1 and at least three named violations.
  - Run: hand-write a 120-word one-pager for the 3-group fixture in `/tmp/op/one-pager.md` whose bullets point at `orchestrator/grouping/plan_edit.py`, `g1`, and `R16`, then `uv run smart-mcps-orchestrate report r20260829-162627 --run-dir tests/fixtures/runs/r20260829-162627 --validate /tmp/op/one-pager.md; echo exit=$?`. Pass: exit 0 with no output; appending the word `Overall` to one bullet flips it to exit 1 naming the banned phrase.
  - Run: `uv run pytest tests/test_report_onepager.py tests/test_finish.py tests/test_cli.py -q`. Pass: each validator rule has a test that trips only it; `finish` with `[docs] formats = ["changelog"]` on the synthetic repo creates a `docs(run): report for <id>` commit on the integration branch before the push call, and with `formats = []` creates none; a failing one-pager aborts `finish` before push with the violations in the error.
- **Edge cases**: —
- **Non-goals / must-not**: `finish` must never commit outside `docs/runs/<run_id>/`; the validator must never rewrite the one-pager.

### U6. Run-driver skill Finish phase, report docs, plugin 0.15.0, committed trial outputs

- **Summary**: The run-driver skill's Finish phase rewritten around `report`, `docs/orchestrator-report.md`, the plugin bump to `0.15.0`, and both fixture runs' complete trial sets (all four formats plus a validated one-pager each) committed under `docs/runs/`.
- **Goal**: `skills/orchestrator-run/SKILL.md` Phase 4 step 4 becomes: run `report --format all`, write `one-pager.md` from `--scaffold`, loop on `--validate` until clean, then `finish`; the "notes as you go" rule stays for triage notes, but the notes file is no longer the record. `docs/orchestrator-report.md` documents the formats, the `[docs]` block, the one-pager contract, the six visualizations, and how the fixture-driven trial was produced. `.claude-plugin/plugin.json` → `0.15.0`. For each fixture run the worker generates `docs/runs/<id>/` with `--format all`, writes a one-pager from the scaffold, validates it to exit 0, and commits the directory; this is the trial set the owner compares.
- **Files**: `skills/orchestrator-run/SKILL.md`, `docs/orchestrator-report.md` *(new, medium)*, `.claude-plugin/plugin.json`
- **Symbols**: —
- **Depends-on**: U5
- **Slice**: —
- **Implements / Consumes**: consumes `report-onepager`
- **Verification**:
  - Run: `uv run smart-mcps-orchestrate report r20260828-220035 --run-dir tests/fixtures/runs/r20260828-220035 --validate docs/runs/r20260828-220035/one-pager.md; echo exit=$?` and the same for `r20260829-162627`. Pass: both committed one-pagers validate with exit 0, and the 11-group one's `## Problems found` bullets point at the four surprises' `report-*.json` paths.
  - Run: `ls docs/runs/r20260829-162627 docs/runs/r20260828-220035 && git status --short docs/runs`. Pass: each directory holds `facts.json`, `CHANGELOG-entry.md`, `pr-body.md`, `report.html`, `one-pager.md`, all tracked with no pending changes.
  - Run: `grep -n "notes-<run_id>.md\|report --format all\|--validate\|--scaffold" skills/orchestrator-run/SKILL.md`. Pass: Phase 4 names `report --format all`, `--scaffold one-pager`, and `--validate`; the notes file is mentioned only as triage notes, not as the summary.
  - Run: `python -c "import json;print(json.load(open('.claude-plugin/plugin.json'))['version'])" && grep -c "^## " docs/orchestrator-report.md`. Pass: `0.15.0`; the doc has sections for formats, config, the one-pager contract, and visualizations (at least 4 `## ` headings).
- **Edge cases**: —
- **Non-goals / must-not**: no change to `/orchestrator-plan` or `/orchestrator-deepen`; no code changes in this unit.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-report-facts
    description: RunFacts model built from export, groups.json, plan sections, junit and the run's git range, plus the `report --format facts` subcommand and a `[docs]` config block
    slice: null
    files:
      - orchestrator/report/__init__.py
      - orchestrator/report/facts.py
      - orchestrator/execution/manifest.py
      - orchestrator/config.py
      - orchestrator/cli.py
      - tests/test_report_facts.py
    size_hints:
      orchestrator/report/__init__.py: small
      orchestrator/report/facts.py: large
      tests/test_report_facts.py: large
    symbols: []
    depends_on: []
    implements: ["report-facts"]
    consumes: []
  - task_id: u2-report-diagrams
    description: Mermaid renderers for the run timeline, plan-to-outcome map, Python import-graph delta between base and tip, and codegraph-walked how-to-use sequences
    slice: null
    files:
      - orchestrator/report/diagrams.py
      - tests/test_report_diagrams.py
    size_hints:
      orchestrator/report/diagrams.py: large
      tests/test_report_diagrams.py: medium
    symbols: []
    depends_on: [u1-report-facts]
    implements: ["report-diagrams"]
    consumes: ["report-facts"]
  - task_id: u3-report-markdown
    description: Run changelog entry from per-group fragments with an idempotent RUNLOG.md section, fixed-template PR body used by finish, and postmortem-lite when the run had trouble
    slice: null
    files:
      - orchestrator/report/markdown.py
      - orchestrator/execution/finish.py
      - orchestrator/cli.py
      - tests/test_report_markdown.py
      - tests/test_finish.py
    size_hints:
      orchestrator/report/markdown.py: large
      tests/test_report_markdown.py: large
    symbols: []
    depends_on: [u1-report-facts, u2-report-diagrams]
    implements: ["report-markdown"]
    consumes: ["report-facts", "report-diagrams"]
  - task_id: u4-report-html
    description: Single-file Jinja2 HTML report with evidence matrix, requirement traceability, per-unit cards and the four mermaid diagrams
    slice: null
    files:
      - orchestrator/report/html.py
      - orchestrator/report/templates/report.html.j2
      - orchestrator/cli.py
      - pyproject.toml
      - tests/test_report_html.py
    size_hints:
      orchestrator/report/html.py: medium
      orchestrator/report/templates/report.html.j2: large
      tests/test_report_html.py: medium
    symbols: []
    depends_on: [u1-report-facts, u2-report-diagrams]
    implements: ["report-html"]
    consumes: ["report-facts", "report-diagrams"]
  - task_id: u5-report-onepager
    description: One-pager scaffold and validator, and finish generating, validating and committing docs/runs/<run_id>/ per [docs] formats before push
    slice: null
    files:
      - orchestrator/report/onepager.py
      - orchestrator/execution/finish.py
      - orchestrator/cli.py
      - tests/test_report_onepager.py
      - tests/test_finish.py
      - tests/test_cli.py
    size_hints:
      orchestrator/report/onepager.py: medium
      tests/test_report_onepager.py: medium
    symbols: []
    depends_on: [u1-report-facts, u3-report-markdown, u4-report-html]
    implements: ["report-onepager"]
    consumes: ["report-facts", "report-markdown", "report-html"]
  - task_id: u6-report-skill-docs
    description: Run-driver skill Finish phase rewritten around report, orchestrator-report.md, plugin 0.15.0, and both fixture runs' trial outputs committed under docs/runs/
    slice: null
    files:
      - skills/orchestrator-run/SKILL.md
      - docs/orchestrator-report.md
      - .claude-plugin/plugin.json
    size_hints:
      docs/orchestrator-report.md: medium
    symbols: []
    depends_on: [u5-report-onepager]
    implements: []
    consumes: ["report-onepager"]
```
