---
title: Orchestrator Observatory — local front-end for run observation and HITL answering
type: feat
date: 2026-07-21
origin: docs/brainstorms/2026-07-21-orchestrator-frontend-requirements.md
---

# Orchestrator Observatory — local front-end for run observation and HITL answering

## Objective

Ship the **Observatory**: a FastAPI/uvicorn backend plus a React/Vite SPA that make an
orchestration run legible while it runs and after it finishes, across multiple projects,
with exactly one write path — answering HITL escalations.

Measured against the origin's requirements: R1–R10 (backend), R11–R17 (SPA), R18–R20
(success criteria). R19 and the board/DAG half of R20 are verified by automated tests in
**U2**, which owns that read surface and the committed post-mortem fixture; R20's
escalation and transcript halves are verified in **U6** and **U8** respectively. **R18 is
a human acceptance gate**, executed against a live `run --hitl` after this plan's work
merges, using the runbook **U10** writes.

## What we already know (resolved context)

Everything below was verified against the working tree on `feat/multiagent-orchestrator`
at plan time. Workers should not re-derive it.

### Run artifacts — the entire read surface

`RunPaths` (`orchestrator/execution/manifest.py:35`) is the single place the layout is
spelled out. For run `<id>` in repo `<repo>`:

| Artifact           | Path                                                 | Notes                                                                |
| ------------------ | ---------------------------------------------------- | -------------------------------------------------------------------- |
| Group states       | `<repo>/.orchestrator/runs/<id>/state.json`          | `RunState`: `groups{gid: {state, generation, failure}}`, `live_pids` |
| Session join       | `<repo>/.orchestrator/runs/<id>/manifest.json`       | `RunManifest`: `groups{gid: {group_name, summary, sessions[]}}`      |
| Event log          | `<repo>/.orchestrator/runs/<id>/logs/run.log`        | Append-only, `O_APPEND`, one timestamped line per event              |
| Escalations        | `<repo>/.orchestrator/runs/<id>/escalations/`        | `request-<id>.json` / `response-<id>.json`; **dir may not exist**    |
| Reports / verdicts | `<repo>/.orchestrator/runs/<id>/groups/<gid>/*.json` | `report-g<N>-r<M>.json`, `verdict-g<N>-r<M>.json`                    |
| DAG                | `<repo>/.orchestrator/groups.json`                   | **Shared, not per-run** — see ADR 0002; U1 adds the per-run snapshot |

- `state.json` and `manifest.json` are written via `atomic_write_text`
  (`manifest.py:22`, write-then-rename), so a reader never sees a torn file. No locking
  is needed on the read side.
- `.orchestrator/` is **gitignored** (`.gitignore:9`). Test fixtures must therefore live
  under `tests/fixtures/`, never as a committed run directory.
- A real post-mortem run exists to develop against — `smoke1` in the sibling worktree
  `/home/gbm1996/wksp/smart_mcps-fe-test/.orchestrator/runs/smoke1/`: two groups, both
  `completed`, four sessions with transcripts, no `escalations/` dir (none ever fired).

### The DAG is not per-run

`_cmd_run` (`orchestrator/cli.py:277`) reads `<repo>/.orchestrator/groups.json`, which
`_cmd_group` (`cli.py:221`) overwrites on every planning cycle. A post-mortem view built
from the shared file renders whatever DAG happens to be on disk today. U1 fixes this by
snapshotting it into the run dir at run start; readers prefer the snapshot and fall back
to the shared file with a `stale_dag` flag. See ADR 0002.

### The HITL channel

- `pending_escalations(paths)` (`orchestrator/execution/escalation.py:135`) already
  implements R6 exactly: globs `request-*.json`, excludes any with a matching
  `response-*.json`, returns `list[EscalationRequest]` sorted by `created_at`, and
  returns `[]` when the directory is absent. R6 is a thin HTTP wrapper over it.
- `EscalationBroker.raise_escalation` (`escalation.py:93`) writes the request, logs an
  `ESCALATION <id> [<kind>] <gid>: <prompt>` line, then polls for `response-<id>.json`.
  Writing that response file **is** the entire answer protocol — no signal, no socket.
- `_cmd_answer` (`cli.py:585`) takes an `argparse.Namespace` and is therefore not
  callable from the server, and it **does not reject an already-answered escalation**,
  which R7 requires. U1 extracts `answer_escalation()` and both callers use it.
- `EscalationKind` has **nine** members (`orchestrator/model.py:145`); `HumanAction` has
  three: `answer`, `skip`, `abort` (`model.py:164`).

### Session transcripts

`SessionEntry.transcript_path` (`model.py:68`) stores an **absolute** path, already
resolved and persisted in `manifest.json`. R8 reads it straight from the manifest; the
`SessionRunner.transcript_path` glob (`sessions.py:229`) is not needed.

Verified transcript shape (106 lines, `smoke1` g2 coder): JSONL, one object per line,
discriminated by a top-level `type`. Observed values: `user`, `assistant`, `attachment`,
`custom-title`, `agent-name`, `mode`, `queue-operation`, `last-prompt`. `assistant`
rows carry `message.content[]` blocks of type `text` / `tool_use`; `user` rows carry
`tool_result` blocks. Rows also carry `sessionId`. The parser must keep only the block
types it renders and skip everything else silently — the format is Claude Code's and
will drift.

### Dependencies — already present

`starlette 1.3.1`, `uvicorn 0.51.0`, `sse-starlette 3.4.5`, `watchfiles 1.2.0`,
`anyio 4.14.1` and `pyyaml 6.0.3` are all installed transitively via `fastmcp>=3.3`.
`fastapi` is **not** — U1 adds it to `pyproject.toml`. Node v22.19.0 / npm 10.9.3 are
available. `dist/` is already gitignored (`.gitignore:5`); `node_modules/` is **not** —
U3 adds it.

### The `ui/` prototype

Eleven files on branch `orchestrator/run-smoke1` (`ui/package.json`, `vite.config.ts`,
`tsconfig.json`, `index.html`, `src/{main,App}.tsx`, `src/{types,sample-run}.ts`,
`src/styles.css`, `src/components/{GroupBoard,EventLog}.tsx`). React 18.3 + Vite 5.4 +
TS 5.5. It is a display shell: `App.tsx` imports `sampleRunState` / `sampleEventLog` /
`sampleEscalations` from `src/sample-run.ts` and renders them. No data layer, no run
selector, no backend.

Its `src/types.ts` has **drifted from the real models** and must be regenerated, not
trusted: `EscalationKind` there is `"too_hard" | "structural" | "blocked" | "needs_input"`, none of which are members of the real nine-value `EscalationKind` enum;
`RunState` omits `live_pids`; `ManifestSession` omits `session_id`, `generation`,
`transcript_path`.

### Branch preconditions — done by a human before this plan runs

This plan is executed on `test/orchestrator-frontend` (worktree
`/home/gbm1996/wksp/smart_mcps-fe-test`, currently at `4fae0ff`). Two git operations are
**not** units — cross-branch merges cannot be sanely performed by workers inside
per-group worktrees:

1. Merge `feat/multiagent-orchestrator` into `test/orchestrator-frontend` (it is 3
   commits short of the HITL escalation flow — U11–U14 — that the Observatory exists to
   exercise).
2. Bring the `ui/` prototype in from `orchestrator/run-smoke1` (e.g.
   `git checkout orchestrator/run-smoke1 -- ui/`).

Until (1) lands, `run --hitl` on that branch has no escalation flow to observe. If `ui/`
is absent when `group` runs, its files are simply flagged prospective — harmless, since
U3 rewrites most of them anyway.

## Decisions

- **FastAPI, not raw Starlette.** Typed request/response models and automatic validation
  on the one write endpoint are worth a single small dependency; `uvicorn`,
  `sse-starlette` and `watchfiles` are already vendored so the delta is exactly one
  package. *Rejected*: raw Starlette (zero new deps, but hand-rolled validation on the
  R7 write path — the one place an unvalidated payload actually matters).

- **The run DAG is snapshotted into the run directory.** `_cmd_run` copies
  `groups.json` to `runs/<id>/groups.json`; readers prefer it and fall back to the
  shared file with a `stale_dag` flag. *Rejected*: always reading the shared file behind
  a warning banner (leaves R20 permanently unreliable), and having the Observatory copy
  on first read (a read-only observer mutating the run dir, and for an old run it
  captures today's wrong DAG). (→ ADR 0002)

- **`answer_escalation()` lives in `orchestrator/execution/escalation.py`.** It sits
  beside `pending_escalations()`, which already owns the request/response pairing rule,
  and both `_cmd_answer` and the HTTP route call it — one implementation of the HITL
  contract, and the CLI inherits the stale-escalation check it currently lacks. This is
  a deliberate, purely additive reading of R10: no existing execution/scheduler/session
  *behavior* changes. *Rejected*: a duplicate in `orchestrator/observatory/` (two
  sources of truth for one contract, explicitly rejected by the brainstorm), and
  extracting into `cli.py` (server importing the CLI module — wrong dependency
  direction).

- **Project Registry: `~/.orchestrator-ui.yaml`, overridable via `--registry PATH`.**
  One registry spans all projects, which is the point of R19; tests pass a `tmp_path`
  file rather than touching `$HOME`; a missing file yields an empty project list with a
  clear message, not a crash. *Rejected*: per-repo `ui.yaml` (R19 would need N synced
  copies), flag-only (no zero-config launch).

- **SPA delivery: vite proxy in dev, serve `ui/dist/` when it exists.** `npm run dev` on
  `:5173` proxies `/api` and `/events` to the backend on `:8765`; `smart-mcps-orchestrate ui` mounts `ui/dist/` as static files if present and otherwise prints the dev recipe.
  `dist/` stays gitignored and no build step is wired into the Python entry point.
  *Rejected*: committing `dist/` (bundle churn in every diff, silent source drift),
  dev-split-only (never a single-command launch).

- **Hub units create the seams; slice units fill them.** U2 creates `events.py` /
  `escalations.py` / `transcripts.py` / `artifacts.py` as stub modules each exporting an
  empty `APIRouter` that `app.py` already includes; U3 likewise creates stub components
  that `App.tsx` already mounts. Consequently **no file is edited by two units** — the
  same quarantine that keeps every `cli.py` change inside U1. *Rejected*: each slice
  adding its own `include_router` line to `app.py` (three worktrees editing one file →
  integration-branch conflicts), and `pkgutil` router auto-discovery in `app.py` (removes
  the shared file entirely, but was **measured to produce a group-DAG cycle**: with U4/U6/
  U8 no longer anchored to U2, the budget splitter carves the slice supernodes apart and
  the resulting groups cycle — see the acyclicity decision below).

  The accepted cost is that U2's shared stub files give it strong affinity with all three
  backend slice units, so the grouper places U1/U2/U4/U6/U8/U10 in **one backend group**
  (~86k of a 100k budget) and splits the front end into three. The slices therefore land
  as layers rather than true verticals. That is a known limitation of the current grouper,
  not of this plan: `depends_on` is ordering-only and contributes no affinity, so nothing
  in the map can pull a backend unit toward its UI counterpart across the Python/TS
  boundary.

- **The two hubs are roots and the doc unit is isolated, so the group DAG cannot cycle.**
  The task DAG being acyclic is not sufficient — Louvain groups tasks, and a group that
  ends up holding both an ancestor and a descendant of a task in another group inverts an
  edge. Two shapes caused exactly that here and were fixed at the plan level (never by
  editing `groups.json`): U3 originally depended on U2, putting the SPA hub *between* the
  backend hub and every UI unit; and the old verification unit had `depends_on` on all
  three slices but **zero affinity edges** — `depends_on` is ordering-only, never affinity
  (`docs/orchestrator-task-map.md`) — so it drifted into the hub cluster that the slices
  then pointed back into. U3 is now a second root, and the doc unit is fully isolated (no
  edges in or out), which no partition can place in a cycle. *Rejected*: merging U2 and U3
  into one hub (guaranteed acyclic, but one oversized cross-stack unit).

- **R18 is a human acceptance gate, not a unit.** A live HITL loop needs a real `claude`
  CLI run in which an escalation genuinely fires; headless workers inside worktrees
  cannot drive that. R19 and R20 are automated in the units that own each read surface
  (U2, U6, U8), and U10 writes the R18 runbook. *Rejected*:
  faking the CLI to automate R18 (proves the endpoint works, not the loop against real
  subagents — precisely the gap the brainstorm says is unverified).

- **Default port 8765, bound to `127.0.0.1`, no auth.** Local single-user dev tool per
  the brainstorm's local-only decision; `--port` overrides.

## Units

### U1. orchestrator-seams — every change inside the existing orchestrator, in one place

- **Goal**: The orchestrator exposes what the Observatory needs — a reusable answer
  function with the stale check, a per-run DAG snapshot, and a `ui` subcommand — with
  all `cli.py` edits quarantined here so no later unit touches it.
- **Files**: `orchestrator/execution/escalation.py`, `orchestrator/execution/manifest.py`,
  `orchestrator/cli.py`, `pyproject.toml`, `tests/test_escalation.py`, `tests/test_cli.py`
- **Symbols**: `pending_escalations`, `_cmd_answer`, `_cmd_run`, `RunPaths`,
  `atomic_write_text`, `EscalationResponse`, `HumanAction`, `GroupingResult`
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `answer_escalation(paths, esc_id, action, text="") -> Path` exists in
    `escalation.py`, writes `response-<id>.json` via `atomic_write_text`, and returns its
    path.
  - It raises `EscalationError` when no `request-<id>.json` exists, and raises
    `EscalationError` when `response-<id>.json` already exists — a test answers the same
    escalation twice and asserts the second call raises and leaves the first response
    file byte-identical.
  - `_cmd_answer` delegates to it, still returns exit code 1 with a message on stderr for
    an unknown escalation id, and now also returns 1 for an already-answered one.
  - `RunPaths.groups_path` returns `<run_dir>/groups.json`.
  - `_cmd_run` writes `groups.json` into the run dir at run start; a test runs the stub
    e2e path and asserts `runs/<id>/groups.json` parses as `GroupingResult` with the same
    group ids as `.orchestrator/groups.json`.
  - `smart-mcps-orchestrate ui --help` exits 0 and lists `--registry`, `--port`, `--repo`.
  - `pyproject.toml` `dependencies` includes `fastapi`; `uv run python -c "import fastapi"` succeeds.
  - The full existing suite still passes — no behavior change to scheduler, review loop,
    or sessions.

### U2. observatory-app-core — the FastAPI app, registry, run discovery, and snapshot

- **Goal**: `create_app()` returns a FastAPI application that lists projects, lists a
  project's runs, and serves a composed run snapshot — reading finished, crashed, and
  live runs identically — with empty stub routers already mounted for the three slices.
  This unit also owns the committed post-mortem fixture and the two cross-cutting
  read-path criteria, **R19** and the board/DAG half of **R20**, because they exercise
  exactly this unit's surface.
- **Files**: `orchestrator/observatory/__init__.py` *(new)*,
  `orchestrator/observatory/app.py` *(new)*, `orchestrator/observatory/registry.py`
  *(new)*, `orchestrator/observatory/runs.py` *(new)*,
  `orchestrator/observatory/events.py` *(new, stub router)*,
  `orchestrator/observatory/escalations.py` *(new, stub router)*,
  `orchestrator/observatory/transcripts.py` *(new, stub router)*,
  `orchestrator/observatory/artifacts.py` *(new, stub router)*,
  `tests/test_observatory_api.py` *(new)*,
  `tests/fixtures/observatory/run-postmortem/state.json` *(new)*,
  `tests/fixtures/observatory/run-postmortem/manifest.json` *(new)*,
  `tests/fixtures/observatory/run-postmortem/groups.json` *(new)*,
  `tests/fixtures/observatory/run-postmortem/logs/run.log` *(new)*
- **Symbols**: `RunPaths`, `RunState`, `RunManifest`, `ManifestStore`, `GroupingResult`,
  `Group`, `GroupRunState`
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: implements `/api/projects`, `/api/runs`,
  `/api/runs/snapshot`
- **Verification**:
  - `GET /api/projects` returns the registry's `{name, repo}` entries in file order; a
    registry path that does not exist yields `200` with `[]`, never a 500.
  - A registry entry whose `repo` is missing or is not a directory is reported with an
    `error` field rather than omitted or crashing the listing.
  - `GET /api/projects/{name}/runs` lists run ids from `<repo>/.orchestrator/runs/`,
    newest first; an absent `runs/` dir yields `[]`.
  - `GET /api/projects/{name}/runs/{run_id}/snapshot` returns one body containing group
    states with generation and failure, the manifest's groups→sessions join, and the DAG
    edges — asserted against a `tests/fixtures/` copy of the `smoke1` run.
  - The snapshot prefers `runs/<id>/groups.json`; when only `.orchestrator/groups.json`
    exists the body sets `stale_dag: true`, and when neither exists it returns the
    snapshot with empty DAG edges and `stale_dag: true` rather than erroring.
  - Nothing in the read path consults `live_pids` liveness or requires a running process:
    a fixture whose groups are all `completed` and one whose group is `failed` both
    return `200` with full bodies.
  - `app.py` mounts `ui/dist/` when present; with no `dist/` the app still starts and
    `GET /` returns a message naming the `npm run dev` recipe.
  - `create_app()` includes the `events`, `escalations`, `transcripts` and `artifacts`
    routers; importing each module and reading `router.routes` succeeds (empty at this
    unit).
  - `tests/fixtures/observatory/run-postmortem/` is a committed copy of a finished run
    (`state.json`, `manifest.json`, `groups.json`, `logs/run.log`), needed because
    `.orchestrator/` is gitignored and cannot supply a fixture.
  - **R19**: one app instance, a registry naming two `tmp_path` repos each holding a
    distinct run, lists both projects and returns each project's own run ids and
    snapshots — no restart, and no run id from one project appearing under the other.
  - **R20 (board/DAG)**: pointed at the post-mortem fixture, the snapshot resolves group
    states, the groups→sessions join and the DAG entirely from disk for a run with no
    live process.
  - A crashed-run fixture — a group left in `running` with a non-empty `live_pids` whose
    pid is not alive — returns a full `200` snapshot rather than erroring or hanging (R9).

### U3. spa-data-layer — SPA shell, typed API client, project and run switchers

- **Goal**: The prototype becomes a real app: fixtures deleted, types regenerated from
  the live models, one typed client for every endpoint this plan defines, a run-change
  hook, and switchers for project and run — with placeholder components already mounted
  for the three slices.
- **Files**: `ui/package.json` *(from prototype)*, `ui/index.html` *(from prototype)*,
  `ui/tsconfig.json` *(from prototype)*, `ui/vite.config.ts` *(from prototype)*,
  `ui/src/main.tsx` *(from prototype)*, `ui/src/App.tsx` *(from prototype)*,
  `ui/src/styles.css` *(from prototype)*, `ui/src/types.ts` *(from prototype,
  regenerated)*, `ui/src/api.ts` *(new)*, `ui/src/useRunStream.ts` *(new)*,
  `ui/src/components/ProjectRunSwitcher.tsx` *(new)*,
  `ui/src/components/GroupBoard.tsx` *(from prototype, reduced to a stub)*,
  `ui/src/components/EventLog.tsx` *(from prototype, reduced to a stub)*,
  `ui/src/components/EscalationPanel.tsx` *(new, stub)*,
  `ui/src/components/GroupDrillIn.tsx` *(new, stub)*, `.gitignore`
- **Symbols**: —
- **Depends-on**: — *(deliberately independent of U2: `api.ts` is written against the
  endpoint contracts stated in this plan's units, not against U2's implementation, and
  nothing in this unit's verification needs a running backend. Keeping the SPA hub off
  U2's downstream side is also what keeps the group DAG acyclic — see the decision
  below.)*
- **Slice**: —
- **Implements / Consumes**: consumes `/api/projects`, `/api/runs`, `/api/runs/snapshot`,
  `/events/log`, `/events/run`, `/api/escalations`, `/api/escalations/answer`,
  `/api/transcripts`, `/api/artifacts` — this unit owns `api.ts`, the single place every
  HTTP contract is used
- **Verification**:
  - `ui/src/sample-run.ts` is deleted and no source file imports it; `rg sample-run ui/src`
    returns nothing.
  - `ui/src/types.ts` matches the live models: `EscalationKind` is the nine-member union
    from `orchestrator/model.py:145`, `HumanAction` is `answer | skip | abort`, `RunState`
    includes `live_pids`, and `ManifestSession` includes `session_id`, `generation` and
    `transcript_path`.
  - `ui/src/api.ts` exports a typed function per endpoint defined in U2, U4, U6 and U8,
    with a single base-URL constant and one shared error path that surfaces the backend's
    message.
  - `ui/src/useRunStream.ts` exports a hook that subscribes to the run-change SSE stream
    and exposes both the latest snapshot and a `revision` counter that slice components
    can depend on.
  - `ProjectRunSwitcher` lists projects from `/api/projects` and, on selection, runs for
    that project; changing either updates the active selection without a page reload.
  - `App.tsx` mounts `GroupBoard`, `EventLog`, `EscalationPanel` and `GroupDrillIn` and
    passes each the active project and run id.
  - `vite.config.ts` proxies `/api` and `/events` to `http://127.0.0.1:8765`.
  - `.gitignore` includes `node_modules/`.
  - `cd ui && npm install && npm run build` exits 0 and produces `ui/dist/index.html`.

### U4. sse-endpoints — live log tail and debounced run-change events

- **Goal**: Two SSE endpoints — one tailing appended `run.log` lines, one emitting a
  lightweight "changed" event when the run directory mutates — so the SPA never
  fixed-interval polls core state.
- **Files**: `orchestrator/observatory/events.py` *(stub created by U2)*,
  `tests/test_observatory_events.py` *(new)*
- **Symbols**: `RunPaths`, `log_event`
- **Depends-on**: U2
- **Slice**: live-board
- **Implements / Consumes**: implements `/events/log`, `/events/run`
- **Verification**:
  - `GET /events/log` streams the existing `run.log` contents first, then each newly
    appended line as its own SSE event; a test appends two lines after connecting and
    receives both in order.
  - A run whose `logs/run.log` does not yet exist yields an open stream that starts
    emitting once the file appears, rather than a 404.
  - `GET /events/run` emits a `changed` event when any of `state.json`,
    `manifest.json`, `escalations/` or `groups/` mutates, and is debounced: a test
    writing `state.json` five times within the debounce window receives fewer events than
    writes, and at least one.
  - Both streams terminate cleanly on client disconnect — a test cancels the request and
    asserts the watcher task stops rather than leaking.
  - Log tailing survives the file being appended to concurrently and never re-emits a
    line already sent.
  - Both endpoints are registered on this module's `router` and are reachable through
    `create_app()` with no edit to `app.py`.

### U5. board-and-log-ui — group board with DAG edges, live event log

- **Goal**: The board renders one card per group from the snapshot with state, generation
  and dependency edges, and the event log renders live from the SSE stream.
- **Files**: `ui/src/components/GroupBoard.tsx`,
  `ui/src/components/GroupBoard.css` *(new)*, `ui/src/components/EventLog.tsx`,
  `ui/src/components/EventLog.css` *(new)*
- **Symbols**: —
- **Depends-on**: U3, U4
- **Slice**: live-board
- **Implements / Consumes**: — *(reaches the backend through U3's `api.ts` and
  `useRunStream`, never over raw HTTP)*
- **Verification**:
  - `GroupBoard` renders one card per group in the snapshot, each showing group id, name,
    state and generation, and shows a group's `failure` text when present.
  - Dependency edges from the snapshot's DAG are rendered on the board, and a snapshot
    with `stale_dag: true` shows a visible "DAG may not match this run" marker.
  - `GroupBoard` re-renders when `useRunStream`'s revision advances, without a manual
    refresh.
  - `EventLog` renders lines from `/events/log` in arrival order and appends new lines as
    they stream, keeping the view pinned to the newest line.
  - Each of the eight `GroupState` values renders with a distinct visual treatment.
  - `npm run build` exits 0 with no TypeScript errors.

### U6. escalation-api — pending escalations and the one write endpoint

- **Goal**: List a run's unanswered escalations and accept an answer, delegating the
  write to U1's `answer_escalation` so validation lives in exactly one place.
- **Files**: `orchestrator/observatory/escalations.py` *(stub created by U2)*,
  `tests/test_observatory_escalations.py` *(new)*
- **Symbols**: `pending_escalations`, `EscalationRequest`, `HumanAction`, `RunPaths`
- **Depends-on**: U2
- **Slice**: hitl
- **Implements / Consumes**: implements `/api/escalations`, `/api/escalations/answer`
- **Verification**:
  - `GET .../escalations` returns each unanswered request's `id`, `kind`, `group_id`,
    `generation`, `prompt`, `created_at` and `context`, sorted by `created_at`; a run
    with an answered escalation excludes it; a run with no `escalations/` dir returns
    `[]`.
  - `POST .../escalations/{esc_id}/answer` with `{"action": "answer", "text": "..."}`
    writes `response-<esc_id>.json`, and a subsequent `pending_escalations` call for that
    run no longer lists it.
  - All three `HumanAction` values are accepted; any other action value is rejected with
    422 and no file is written.
  - Answering an unknown escalation id returns 404; answering an already-answered one
    returns 409 and leaves the existing response file byte-identical.
  - The route calls `answer_escalation` — the test asserts the response file matches what
    the CLI's `answer` subcommand produces for the same inputs, field for field except
    `answered_at`.
  - Both endpoints are registered on this module's `router` and are reachable through
    `create_app()` with no edit to `app.py`.

### U7. escalation-ui — pending escalations panel with an inline answer form

- **Goal**: Pending escalations are visible with their full prompt and context, and can
  be resolved from the UI with answer / skip / abort plus free text.
- **Files**: `ui/src/components/EscalationPanel.tsx`,
  `ui/src/components/EscalationPanel.css` *(new)*
- **Symbols**: —
- **Depends-on**: U3, U6
- **Slice**: hitl
- **Implements / Consumes**: — *(reaches the backend through U3's `api.ts`)*
- **Verification**:
  - The panel lists each pending escalation with its kind, group id, generation and full
    prompt text, and renders the request's `context` pointers when present.
  - Each entry offers answer / skip / abort and a free-text field; submitting posts to
    the R7 endpoint with the correct correlation id.
  - The panel clears the resolved entry once the next run-change revision arrives from
    `useRunStream`, rather than optimistically removing it.
  - A 409 from the backend surfaces an "already answered" message and refreshes the list
    instead of silently failing.
  - The submit control is disabled while a request is in flight, so double-submitting one
    escalation is not possible.
  - A run with no pending escalations renders an explicit empty state, not a blank area.
  - `npm run build` exits 0 with no TypeScript errors.

### U8. transcript-api — tolerant `.jsonl` parser and group artifact endpoints

- **Goal**: Serve a normalized event list parsed from a session's Claude transcript, and
  serve a group's report/verdict JSON, both resolved from `manifest.json`.
- **Files**: `orchestrator/observatory/transcripts.py` *(stub created by U2)*,
  `orchestrator/observatory/artifacts.py` *(stub created by U2)*,
  `tests/test_observatory_transcripts.py` *(new)*,
  `tests/fixtures/observatory/transcript.jsonl` *(new)*
- **Symbols**: `RunManifest`, `SessionEntry`, `CoderReport`, `ReviewerVerdict`, `RunPaths`
- **Depends-on**: U2
- **Slice**: drill-in
- **Implements / Consumes**: implements `/api/transcripts`, `/api/artifacts`
- **Verification**:
  - `GET .../sessions/{session_id}/transcript` resolves `transcript_path` from
    `manifest.json` and returns a normalized list of `{seq, role, kind, text|tool_name| tool_input|tool_result}` entries.
  - The parser handles the verified shape: `assistant` rows' `message.content[]` blocks
    of type `text` and `tool_use`, and `user` rows' `tool_result` blocks.
  - Unknown row types are skipped silently — a fixture containing `attachment`,
    `custom-title`, `agent-name`, `mode`, `queue-operation`, `last-prompt` and a
    fabricated `future-event-type` row parses without error and yields only the
    renderable events.
  - A malformed (non-JSON) line is skipped and the remaining lines still parse — the
    endpoint never 500s on a partially written transcript.
  - A session whose `transcript_path` is null or points at a missing file returns 404
    with a message naming the session id, not a stack trace.
  - Each call re-reads the file, so a test appending rows between two calls sees the new
    events on the second — this is what makes the SPA's re-poll work.
  - `GET .../groups/{group_id}/artifacts` lists `groups/<gid>/*.json` with their parsed
    contents, and a group directory that does not exist returns `[]`.
  - Every endpoint above is registered on its module's `router` and is reachable through
    `create_app()` with no edit to `app.py`.

### U9. drill-in-ui — per-group transcript pane with report and verdict

- **Goal**: Clicking a group opens a pane showing its agents' transcripts, refreshed
  while open, alongside that group's finished reports and verdicts.
- **Files**: `ui/src/components/GroupDrillIn.tsx`,
  `ui/src/components/GroupDrillIn.css` *(new)*
- **Symbols**: —
- **Depends-on**: U3, U8
- **Slice**: drill-in
- **Implements / Consumes**: — *(reaches the backend through U3's `api.ts`)*
- **Verification**:
  - Selecting a group opens the pane and lists that group's sessions from the snapshot
    with role and generation; selecting a session loads its transcript.
  - Assistant text, tool calls (name plus input) and tool results render as visually
    distinct entries.
  - The transcript re-fetches on an interval while the pane is open and stops fetching
    when it closes or the selected session changes — verified by asserting no further
    requests are issued after close.
  - The pane renders the group's `report-*.json` and `verdict-*.json` contents, including
    `status`, `summary`/`notes`, `verification_results` and `surprises`.
  - A group with no sessions yet, and a session whose transcript 404s, each render an
    explicit message rather than an empty pane or a crash.
  - `npm run build` exits 0 with no TypeScript errors.

### U10. observatory-docs — operator documentation and the R18 live HITL runbook

- **Goal**: An operator can register projects, launch the Observatory in either mode, and
  execute the R18 live acceptance gate from written instructions.
- **Files**: `docs/observatory.md` *(new)*, `orchestrator/README.md`
- **Symbols**: `RunPaths`
- **Depends-on**: — *(documents contracts this plan defines rather than code another unit
  produces, so it carries no dependency edge and nothing depends on it — see the decision
  below on why that isolation matters)*
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `docs/observatory.md` documents the registry file format, the dev recipe (backend on
    `:8765` plus `npm run dev` on `:5173`), the build-and-serve recipe, and every endpoint
    this plan defines.
  - `docs/observatory.md` contains the **R18 live HITL runbook**: register a project, start
    `smart-mcps-orchestrate run --hitl`, open the Observatory, wait for an escalation to
    appear, answer it from the UI, and confirm from `run.log` and the board that the group
    resumed — with the expected observation at each step.
  - `orchestrator/README.md` links to `docs/observatory.md` and documents
    `runs/<id>/groups.json` as a run artifact.
  - Every endpoint documented matches the paths this plan's units declare — no endpoint
    is documented that no unit implements, and none implemented is left undocumented.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-orchestrator-seams
    description: Add answer_escalation with the stale check, RunPaths.groups_path, the per-run groups.json snapshot, the ui subcommand, and the fastapi dependency
    slice: null
    files:
      - orchestrator/execution/escalation.py
      - orchestrator/execution/manifest.py
      - orchestrator/cli.py
      - pyproject.toml
      - tests/test_escalation.py
      - tests/test_cli.py
    symbols:
      - pending_escalations
      - _cmd_answer
      - _cmd_run
      - RunPaths
      - atomic_write_text
      - EscalationResponse
      - HumanAction
      - GroupingResult
    depends_on: []
    implements: []
    consumes: []
  - task_id: u2-observatory-app-core
    description: FastAPI app factory with project registry, run discovery, the composed run snapshot, static SPA mount, and empty stub routers for the three slices
    slice: null
    files:
      - orchestrator/observatory/__init__.py
      - orchestrator/observatory/app.py
      - orchestrator/observatory/registry.py
      - orchestrator/observatory/runs.py
      - orchestrator/observatory/events.py
      - orchestrator/observatory/escalations.py
      - orchestrator/observatory/transcripts.py
      - orchestrator/observatory/artifacts.py
      - tests/test_observatory_api.py
      - tests/fixtures/observatory/run-postmortem/state.json
      - tests/fixtures/observatory/run-postmortem/manifest.json
      - tests/fixtures/observatory/run-postmortem/groups.json
      - tests/fixtures/observatory/run-postmortem/logs/run.log
    symbols:
      - RunPaths
      - RunState
      - RunManifest
      - ManifestStore
      - GroupingResult
      - Group
      - GroupRunState
    depends_on: [u1-orchestrator-seams]
    implements: ["/api/projects", "/api/runs", "/api/runs/snapshot"]
    consumes: []
  - task_id: u3-spa-data-layer
    description: Replace the prototype fixtures with a typed API client, regenerated types, a run-change hook, project and run switchers, and stub components for the three slices
    slice: null
    files:
      - ui/package.json
      - ui/index.html
      - ui/tsconfig.json
      - ui/vite.config.ts
      - ui/src/main.tsx
      - ui/src/App.tsx
      - ui/src/styles.css
      - ui/src/types.ts
      - ui/src/api.ts
      - ui/src/useRunStream.ts
      - ui/src/components/ProjectRunSwitcher.tsx
      - ui/src/components/GroupBoard.tsx
      - ui/src/components/EventLog.tsx
      - ui/src/components/EscalationPanel.tsx
      - ui/src/components/GroupDrillIn.tsx
      - .gitignore
    symbols: []
    depends_on: []
    implements: []
    consumes:
      - "/api/projects"
      - "/api/runs"
      - "/api/runs/snapshot"
      - "/events/log"
      - "/events/run"
      - "/api/escalations"
      - "/api/escalations/answer"
      - "/api/transcripts"
      - "/api/artifacts"
  - task_id: u4-sse-endpoints
    description: SSE endpoints tailing run.log and emitting debounced run-directory change events
    slice: live-board
    files:
      - orchestrator/observatory/events.py
      - tests/test_observatory_events.py
    symbols:
      - RunPaths
      - log_event
    depends_on: [u2-observatory-app-core]
    implements: ["/events/log", "/events/run"]
    consumes: []
  - task_id: u5-board-and-log-ui
    description: Group board rendering states, generations and DAG edges from the snapshot, plus the live event log
    slice: live-board
    files:
      - ui/src/components/GroupBoard.tsx
      - ui/src/components/GroupBoard.css
      - ui/src/components/EventLog.tsx
      - ui/src/components/EventLog.css
    symbols: []
    depends_on: [u3-spa-data-layer, u4-sse-endpoints]
    implements: []
    consumes: []
  - task_id: u6-escalation-api
    description: Pending-escalation listing and the answer write endpoint delegating to answer_escalation
    slice: hitl
    files:
      - orchestrator/observatory/escalations.py
      - tests/test_observatory_escalations.py
    symbols:
      - pending_escalations
      - EscalationRequest
      - HumanAction
      - RunPaths
    depends_on: [u2-observatory-app-core]
    implements: ["/api/escalations", "/api/escalations/answer"]
    consumes: []
  - task_id: u7-escalation-ui
    description: Pending escalations panel with an inline answer, skip and abort form
    slice: hitl
    files:
      - ui/src/components/EscalationPanel.tsx
      - ui/src/components/EscalationPanel.css
    symbols: []
    depends_on: [u3-spa-data-layer, u6-escalation-api]
    implements: []
    consumes: []
  - task_id: u8-transcript-api
    description: Tolerant Claude transcript jsonl parser plus group report and verdict artifact endpoints
    slice: drill-in
    files:
      - orchestrator/observatory/transcripts.py
      - orchestrator/observatory/artifacts.py
      - tests/test_observatory_transcripts.py
      - tests/fixtures/observatory/transcript.jsonl
    symbols:
      - RunManifest
      - SessionEntry
      - CoderReport
      - ReviewerVerdict
      - RunPaths
    depends_on: [u2-observatory-app-core]
    implements: ["/api/transcripts", "/api/artifacts"]
    consumes: []
  - task_id: u9-drill-in-ui
    description: Per-group drill-in pane rendering agent transcripts on a poll plus that group's reports and verdicts
    slice: drill-in
    files:
      - ui/src/components/GroupDrillIn.tsx
      - ui/src/components/GroupDrillIn.css
    symbols: []
    depends_on: [u3-spa-data-layer, u8-transcript-api]
    implements: []
    consumes: []
  - task_id: u10-observatory-docs
    description: Operator documentation for the registry, dev and build recipes, every endpoint, and the R18 live HITL runbook
    slice: null
    files:
      - docs/observatory.md
      - orchestrator/README.md
    symbols:
      - RunPaths
    depends_on: []
    implements: []
    consumes: []
```
