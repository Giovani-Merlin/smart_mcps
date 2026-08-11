# Orchestrator Observatory

A local, single-user web tool for watching an orchestration run — while it runs
and after it finishes — across every project you register, with exactly one write
path: answering a run's human-in-the-loop (HITL) escalations.

It is a **reader of run directories**. A run is a directory on disk, not a
process, so the Observatory renders finished runs, failed runs, and runs whose
orchestrator crashed mid-flight identically — it never needs a live process and
never checks whether a recorded worker PID is still alive. The backend binds
`127.0.0.1`, defaults to port `8765`, and has **no authentication**: it is meant
to run on your own machine only.

- Backend: a FastAPI app (`orchestrator/observatory/`), launched with
  `smart-mcps-orchestrate ui`.
- Frontend: a React/Vite SPA under `ui/`.

## The project registry

One YAML file names every repo the Observatory watches, so a single running
instance serves runs from several projects without a restart. Its default
location is `~/.orchestrator-ui.yaml`; override it with `--registry PATH` (tests
point this at a temp file so they never touch `$HOME`).

```yaml
# ~/.orchestrator-ui.yaml
projects:
  - name: smart-mcps          # optional; defaults to the repo directory name
    repo: /home/you/wksp/smart-mcps
  - name: other-service
    repo: ~/code/other-service # ~ is expanded
```

A bare top-level list (without the `projects:` key) is also accepted, since that
is what people write from memory.

Behaviour at the edges — all deliberate, none of them a crash:

- **No registry file** → an empty project list (`[]`). The Observatory is often
  the first thing you launch, before any registry exists. If you launched with
  `smart-mcps-orchestrate ui` inside a repo, that repo is offered as a single
  zero-config fallback project (see `--repo` below) *only* when no registry file
  is present.
- **A registry that is empty or not valid YAML** → an empty list (or a single
  entry carrying an `error`), never a 500.
- **An entry whose `repo` is missing, does not exist, or is not a directory** →
  the entry is still listed, but with its `error` field set, so a typo is
  visible in the UI instead of silently shortening the list.

## Running it

### `smart-mcps-orchestrate ui`

```sh
smart-mcps-orchestrate ui [--registry PATH] [--port N] [--repo DIR]
```

| Flag         | Default                   | Meaning                                                             |
| ------------ | ------------------------- | ------------------------------------------------------------------- |
| `--registry` | `~/.orchestrator-ui.yaml` | Project registry YAML.                                              |
| `--port`     | `8765`                    | Port on `127.0.0.1`.                                                |
| `--repo`     | current directory         | Repo served as a fallback project **only** when no registry exists. |

The server always exposes the JSON API and the two SSE streams. Whether it also
serves the SPA depends on whether a built bundle exists (below).

### Dev recipe — backend + Vite dev server

Two terminals. Nothing is built; the SPA is served by Vite with hot reload and
proxies API/SSE calls to the backend.

```sh
# terminal 1 — the API on :8765
smart-mcps-orchestrate ui --registry ~/.orchestrator-ui.yaml

# terminal 2 — the SPA on :5173, proxying /api and /events to :8765
cd ui
npm install
npm run dev
```

Open **http://127.0.0.1:5173**. `ui/vite.config.ts` proxies `/api` and `/events`
to `http://127.0.0.1:8765`, so the SPA and backend behave as one origin.

### Build-and-serve recipe — single origin

Build the SPA once; the backend then serves it from `/` on the same port as the
API, so there is no second process.

```sh
cd ui
npm install
npm run build        # produces ui/dist/index.html and assets

smart-mcps-orchestrate ui   # detects ui/dist/ and mounts it at /
```

Open **http://127.0.0.1:8765**. `ui/dist/` is gitignored and no build step is
wired into the Python entry point, so a fresh checkout legitimately has no
bundle. In that case the server still starts and `GET /` returns a JSON message
naming this dev recipe rather than a 404.

### URL scheme

Every view is a URL, so a view can be linked, bookmarked and refreshed. Path
segments identify **objects**; query params identify **view state**.

| URL                                          | View                                     |
| -------------------------------------------- | ---------------------------------------- |
| `/`                                          | Project picker                           |
| `/p/:project`                                | Run index — every run, newest first      |
| `/p/:project/r/:run/board`                   | Board                                    |
| `/p/:project/r/:run/history`                 | Attempt history grid                     |
| `/p/:project/r/:run/grouping`                | How this plan became groups              |
| `/p/:project/r/:run/escalations`             | Pending escalations (the one write path) |
| `/p/:project/r/:run/log`                     | `run.log`, full height                   |
| `/p/:project/r/:run/cost`                    | Estimate vs actual                       |
| `/p/:project/r/:run/session/:group/:session` | Session viewer — addressable, not a tab  |

View state rides in `?group=`, `?stage=`, `?edge=`, `?seq=` and survives
navigation between tabs.

The run index exists to fix a specific dead end: selecting a project used to
jump straight to whichever run was newest, which left older runs unreachable
and silently redirected shared deep links. It does not redirect.

Deep links depend on the backend's SPA catch-all (`_mount_spa` in
`observatory/app.py`), which serves `index.html` for any path outside a route
prefix the server owns. Without it a refresh on `/p/proj/r/run/grouping` would
404.

## HTTP API

Every run-scoped endpoint is prefixed `/api/projects/{project}/runs/{run_id}`.
`{project}` is a registry entry's `name`; `{run_id}` is a directory under
`<repo>/.orchestrator/runs/`.

### Projects and runs

| Method & path                                        | Returns                                                                                 |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `GET /api/projects`                                  | Registry entries in file order: `[{name, repo, error}]`. Missing registry → `[]`.       |
| `GET /api/projects/{project}/runs`                   | Run ids for the project, newest first: `[{run_id, updated_at}]`. No `runs/` dir → `[]`. |
| `GET /api/projects/{project}/runs/{run_id}/snapshot` | The composed run snapshot (below). Unknown project or run → `404`.                      |

**The snapshot** is one body with everything the board renders, composed from
disk in a single request:

```jsonc
{
  "project": "smart-mcps",
  "run_id": "smoke1",
  "plan_path": "...",
  "base_session_id": "...",
  "created_at": "...",
  "groups": [
    {
      "group_id": "g1",
      "name": "observatory-backend",
      "summary": "...",
      "state": "completed",        // pending | ready | running | ... (8 GroupState values)
      "generation": 1,
      "failure": null,             // failure text when the group failed
      "depends_on": ["g0"],
      "sessions": [
        {"session_id": "...", "role": "coder", "generation": 1,
         "name": "...", "retirement_reason": null, "transcript_path": "/abs/path.jsonl"}
      ]
    }
  ],
  "edges": [{"from": "g0", "to": "g1"}],  // DAG dependency edges
  "stale_dag": false,
  "live_pids": {}                          // recorded for display only; never checked for liveness
}
```

- **State + generation + failure** come from `state.json`; the
  **groups → sessions join** comes from `manifest.json`; the **DAG edges** come
  from the run's own `runs/<id>/groups.json` snapshot.
- **`stale_dag`** — the per-run `groups.json` is preferred. `.orchestrator/groups.json`
  is shared across runs and rewritten by every planning cycle, so when only that
  shared file is available the snapshot falls back to it and sets `stale_dag: true`.
  When neither exists the snapshot still returns `200` with `edges: []` and
  `stale_dag: true` rather than erroring.

### Escalations (the one write path)

| Method & path                                                            | Returns                                                          |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| `GET  /api/projects/{project}/runs/{run_id}/escalations`                 | Unanswered requests, oldest first. No `escalations/` dir → `[]`. |
| `POST /api/projects/{project}/runs/{run_id}/escalations/{esc_id}/answer` | Writes the response file that unblocks the group.                |

Each listed escalation carries `{id, run_id, group_id, generation, kind, prompt, context, created_at}`; answered requests are excluded.

The answer body is `{"action": "answer" | "skip" | "abort", "text": "..."}`:

- `answer` (with `text` guidance) resumes/guides the blocked group; `skip` fails
  the group; `abort` stops the run.
- On success the response file `escalations/response-<esc_id>.json` is written and
  the escalation stops appearing as pending. Writing that file **is** the entire
  answer protocol — no signal, no socket — so a successful POST is what unblocks
  the run.
- This route delegates to the same `answer_escalation()` the CLI's `answer`
  subcommand calls, so a UI answer and a CLI answer produce a byte-identical
  response file (bar the timestamp).

Error mapping:

| Condition                        | Status |
| -------------------------------- | ------ |
| Unknown escalation id            | `404`  |
| Already answered (first stands)  | `409`  |
| Action outside answer/skip/abort | `422`  |

A `409` or `422` writes nothing and leaves any existing response file untouched.

### Transcripts and artifacts (per-group drill-in)

| Method & path                                                                | Returns                                                 |
| ---------------------------------------------------------------------------- | ------------------------------------------------------- |
| `GET /api/projects/{project}/runs/{run_id}/sessions/{session_id}/transcript` | Normalized transcript events for the session.           |
| `GET /api/projects/{project}/runs/{run_id}/groups/{group_id}/artifacts`      | The group's `report-*.json` / `verdict-*.json`, parsed. |

**Transcript** — the session's `transcript_path` is resolved from `manifest.json`
(it is absolute and already on disk) and the Claude Code `.jsonl` is normalized to
`[{seq, role, kind, text?, tool_name?, tool_input?, tool_result?, is_error, timestamp?}]`,
covering `assistant` rows' `text`/`tool_use` blocks and `user` rows' `tool_result`
blocks. The parser is **tolerant by construction**: it keeps only the block types
the drill-in renders and silently drops every other row type and every line that
does not parse, because the format is Claude Code's and will drift. Each call
re-reads the file, so a poll picks up new turns while a session is still writing.
A session whose `transcript_path` is null or points at a missing file, or which is
not in the manifest, returns `404` naming the session id — never a stack trace.

**Artifacts** — `groups/<gid>/*.json` in filename order, each as
`{name, kind, content, error?}` where `kind` is `report` / `verdict` / `other`.
Contents are parsed but not schema-validated, so an artifact written by an older
schema is still readable. A group directory that does not exist yet → `[]`.

### SSE streams

Both take `project` and `run` as **query parameters** (not path segments):

| Method & path                     | Emits                                                                                                                                                                       |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /events/log?project=P&run=R` | Unnamed SSE messages, one per `run.log` line: the existing backlog first, then each newly appended line, never re-emitting one already sent.                                |
| `GET /events/run?project=P&run=R` | Named `changed` events (data = run id) when `state.json`, `manifest.json`, `escalations/` or `groups/` mutate — **debounced**, so a burst of writes collapses to one event. |

Both open successfully for artifacts that do not exist yet (a client that connects
before `run.log` exists gets an open stream, not a 404, and starts receiving once
the file appears), and both tear their watcher down cleanly when the client
disconnects. The SPA uses `/events/run` to know *when* to re-fetch the snapshot
(the snapshot endpoint stays the single composition point) and `/events/log` to
tail the live event log.

### Other routes

- `GET /` — the built SPA (`ui/dist/`) when present, otherwise a JSON message with
  the dev recipe.
- FastAPI additionally serves interactive API docs at `/docs` and the schema at
  `/openapi.json` — these are the framework's defaults, not part of this tool.

## R18 — live HITL acceptance runbook

This is the human acceptance gate: prove that a real `run --hitl` escalation
appears in the Observatory, is answered from the UI, and resumes the blocked
group. It needs a real `claude` CLI run in which an escalation genuinely fires, so
it is executed by a person after this work merges — it is not automated.

**Prerequisites**: the SPA is built or the Vite dev server is running; you have a
plan grouped in a target repo (`smart-mcps-orchestrate group <plan.md>`), and a
plan/tasks shaped so a coder will hit a genuine `needs_input` moment.

1. **Register the project.** Add the repo to `~/.orchestrator-ui.yaml`:

   ```yaml
   projects:
     - name: myproj
       repo: /abs/path/to/myproj
   ```

   *Expected:* `GET /api/projects` (and the project switcher) lists `myproj` with
   no `error`.

2. **Start the Observatory** in one terminal:

   ```sh
   smart-mcps-orchestrate ui
   ```

   *Expected:* it prints `Observatory on http://127.0.0.1:8765`. Open the SPA
   (`:8765` if built, `:5173` under the dev recipe).

3. **Launch a HITL run** in a second terminal, sending output to a log you can
   tail:

   ```sh
   cd /abs/path/to/myproj
   smart-mcps-orchestrate run --hitl 2>&1 | tee /tmp/myproj-run.log
   ```

   *Expected:* the run starts; note its run id from the output (or from the run
   switcher, which lists it newest-first). The group board renders one card per
   group with its state and generation, and the DAG edges between them.

4. **Select the run in the UI** and watch the board. As the run progresses the
   board and event log update live (driven by `/events/run` and `/events/log`) —
   you do not refresh the page. *Expected:* group cards move from `pending` →
   `ready` → `running`; the event log tails `run.log` line by line.

5. **Wait for an escalation to appear.** When a coder reports `needs_input`, the
   orchestrator writes `escalations/request-<id>.json`, logs an
   `ESCALATION <id> ...` line, and blocks that group. *Expected:* the escalation
   panel shows a new pending entry with its kind, group id, generation, full
   prompt and any context pointers; the run log shows the `ESCALATION` line; the
   group's card shows it is blocked. Sibling groups keep running.

6. **Answer it from the UI.** In the escalation panel, choose `answer` (with
   guidance text), `skip`, or `abort`, and submit. *Expected:* the POST succeeds;
   `escalations/response-<id>.json` is written; on the next `changed` revision the
   entry disappears from the pending list. Answering the same escalation again
   returns `409` and the UI shows "already answered".

7. **Confirm resumption.** *Expected:* `run.log` shows the group resuming after
   the answer, and the board shows the group leaving its blocked state (back to
   `running`, then eventually `completed`/`changes_required`/etc.). The run
   continues to completion or to the next escalation.

If every *Expected* holds — the escalation appeared, was answered from the UI, and
the group demonstrably resumed from that answer — R18 passes.

## Testing

The backend suite runs offline: `uv run pytest tests/test_observatory_*.py`. It
reads from `tmp_path` or from the committed post-mortem fixture under
`tests/fixtures/observatory/run-postmortem/` (a copy of a finished run, needed
because `.orchestrator/` is gitignored and cannot itself be a committed fixture).
No test touches `$HOME` and none needs a running orchestrator.

The frontend suite is vitest + testing-library, configured in
`ui/vite.config.ts` and run with:

```sh
cd ui
npm install
npm test          # vitest run — no watcher, exits non-zero on failure
npm run build     # tsc --noEmit equivalent, then the production bundle
```

It covers the surfaces that rot first: the route shape (every tab reachable by
URL, query params round-tripping), the `GroupState` → colour map's
exhaustiveness, and `PathChip`'s copy behaviour. The map's compile-time guard is
a `Record<GroupState, StatusStyle>`, so adding a state to the union without
giving it a colour fails `npm run build` rather than rendering a blank badge.
