// The single place every backend HTTP contract is used. Components never call
// `fetch` or construct an `EventSource` themselves — they go through the typed
// functions here (and `useRunStream`), so there is exactly one base URL and one
// error path. Written against the endpoint contracts the plan defines, not
// against a running backend: this layer compiles and type-checks with the server
// down.
//
// Endpoint map (all under the vite dev proxy, see vite.config.ts):
//   GET  /api/projects
//   GET  /api/projects/{project}/runs
//   GET  /api/projects/{project}/runs/{run}/snapshot
//   GET  /api/projects/{project}/runs/{run}/escalations
//   POST /api/projects/{project}/runs/{run}/escalations/{esc}/answer
//   GET  /api/projects/{project}/runs/{run}/sessions/{session}/transcript
//   GET  /api/projects/{project}/runs/{run}/groups/{group}/artifacts
//   GET  /api/projects/{project}/runs/{run}/groups/{group}/diff
//   GET  /api/projects/{project}/runs/{run}/groups/{group}/generations/{gen}/diff
//   GET  /api/projects/{project}/runs/{run}/grouping/llm
//   GET  /api/projects/{project}/runs/{run}/grouping/llm/calls/{seq}
//   GET  /api/projects/{project}/runs/{run}/paths
//   GET  /api/projects/{project}/plans
//   GET  /api/projects/{project}/groupings
//   GET  /api/projects/{project}/jobs[/{job}]
//   POST /api/projects/{project}/jobs/{group|run|resume}
//   SSE  /events/log?project=&run=
//   SSE  /events/run?project=&run=
//   SSE  /events/job?project=&job=

import type {
  AnswerBody,
  AnswerResult,
  Artifact,
  DiffResult,
  EscalationRequest,
  GroupJobBody,
  GroupingPreview,
  GroupingSummary,
  GroupingView,
  JobInfo,
  LlmCallDetail,
  LlmCallsView,
  PlanDoc,
  Project,
  ResumeJobBody,
  RunInfo,
  RunJobBody,
  RunPathsView,
  RunSnapshot,
  TranscriptEvent,
} from "./types";

// Same-origin: dev serves the SPA through vite's proxy, prod serves it from the
// FastAPI static mount, so a relative base works in both without configuration.
export const API_BASE = "";

/** Every non-2xx response from the backend surfaces as one of these, carrying
 * the backend's own `detail` message so callers can show it verbatim. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The one place a caught error becomes display text — components render this,
 * never their own `String(err)` variants. */
export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

async function backendMessage(res: Response): Promise<string> {
  // FastAPI errors are `{"detail": ...}`; fall back to status text otherwise.
  try {
    const body = await res.json();
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") return detail;
    if (detail !== undefined) return JSON.stringify(detail);
  } catch {
    // Body was empty or not JSON — use the status line below.
  }
  return res.statusText || `request failed (${res.status})`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, init);
  if (!res.ok) {
    throw new ApiError(res.status, await backendMessage(res));
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

function runPath(project: string, run: string): string {
  return `/api/projects/${enc(project)}/runs/${enc(run)}`;
}

// --------------------------------------------------------------------- REST

export function listProjects(): Promise<Project[]> {
  return request<Project[]>("/api/projects");
}

export function listRuns(project: string): Promise<RunInfo[]> {
  return request<RunInfo[]>(`/api/projects/${enc(project)}/runs`);
}

export function getSnapshot(project: string, run: string): Promise<RunSnapshot> {
  return request<RunSnapshot>(`${runPath(project, run)}/snapshot`);
}

export function listEscalations(project: string, run: string): Promise<EscalationRequest[]> {
  return request<EscalationRequest[]>(`${runPath(project, run)}/escalations`);
}

export function answerEscalation(
  project: string,
  run: string,
  escId: string,
  body: AnswerBody,
): Promise<AnswerResult> {
  return request<AnswerResult>(`${runPath(project, run)}/escalations/${enc(escId)}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/**
 * A session's transcript. Pass `afterSeq` — the highest `seq` already held —
 * to fetch only what is new; the poll was otherwise re-downloading a whole
 * 342-turn transcript every three seconds. `seq` is stable across full and
 * incremental fetches, so the two can be concatenated directly.
 */
export function getTranscript(
  project: string,
  run: string,
  sessionId: string,
  afterSeq = 0,
): Promise<TranscriptEvent[]> {
  const query = afterSeq > 0 ? `?after_seq=${afterSeq}` : "";
  return request<TranscriptEvent[]>(
    `${runPath(project, run)}/sessions/${enc(sessionId)}/transcript${query}`,
  );
}

/** Everything the Grouping tab renders, in one request. Never 404s on a
 * missing artifact — an absent trace is the normal state of an older run, and
 * the response says which artifact was missing and where it was looked for. */
export function getGrouping(project: string, run: string): Promise<GroupingView> {
  return request<GroupingView>(`${runPath(project, run)}/grouping`);
}

/** The grouper's own LLM call index — mapper and speccer, grouping-time and
 * rewrite alike. 200 with `present: false` when there is none: a run made
 * before the call recorder shipped, or a task map read verbatim with no model
 * call at all. */
export function getLlmCalls(project: string, run: string): Promise<LlmCallsView> {
  return request<LlmCallsView>(`${runPath(project, run)}/grouping/llm`);
}

/** One recorded call in full — the prompt it sent and the raw text it got
 * back — for the session viewer to render. */
export function getLlmCall(project: string, run: string, seq: number): Promise<LlmCallDetail> {
  return request<LlmCallDetail>(`${runPath(project, run)}/grouping/llm/calls/${seq}`);
}

/** The run's on-disk paths, display-only. Every file-backed panel header takes
 * its `PathChip` from here rather than rebuilding a path client-side, so a
 * layout change on the server moves the chips with it. */
export function getRunPaths(project: string, run: string): Promise<RunPathsView> {
  return request<RunPathsView>(`${runPath(project, run)}/paths`);
}

export function getArtifacts(
  project: string,
  run: string,
  groupId: string,
): Promise<Artifact[]> {
  return request<Artifact[]>(`${runPath(project, run)}/groups/${enc(groupId)}/artifacts`);
}

/** A completed group's whole diff against the integration tip it branched
 * from (plan U29, R4). Never 404s — a torn-down branch or an unmerged group
 * comes back `available: false` with a `reason`. */
export function getGroupDiff(project: string, run: string, groupId: string): Promise<DiffResult> {
  return request<DiffResult>(`${runPath(project, run)}/groups/${enc(groupId)}/diff`);
}

/** A generation's final diff (plan U29, R3) — the commits its coder session
 * made, not a running feed. Same never-404s contract as `getGroupDiff`. */
export function getGenerationDiff(
  project: string,
  run: string,
  groupId: string,
  generation: number,
): Promise<DiffResult> {
  return request<DiffResult>(
    `${runPath(project, run)}/groups/${enc(groupId)}/generations/${generation}/diff`,
  );
}

// -------------------------------------------------------------------- launch

function projectPath(project: string): string {
  return `/api/projects/${enc(project)}`;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** Plan documents the launch form's picker offers. Paths come back relative to
 * the repo and are accepted back the same way — the CLI re-anchors them. */
export function listPlans(project: string): Promise<PlanDoc[]> {
  return request<PlanDoc[]>(`${projectPath(project)}/plans`);
}

export function listGroupings(project: string): Promise<GroupingSummary[]> {
  return request<GroupingSummary[]>(`${projectPath(project)}/groupings`);
}

/** A named grouping's own groups.json, read-only — what the launch page shows
 * without a throwaway `group --dry-run`. Never errors on an absent
 * groups.json; `present: false` carries the explanation instead. */
export function getGroupingPreview(project: string, name: string): Promise<GroupingPreview> {
  return request<GroupingPreview>(`${projectPath(project)}/groupings/${enc(name)}/preview`);
}

export function listJobs(project: string): Promise<JobInfo[]> {
  return request<JobInfo[]>(`${projectPath(project)}/jobs`);
}

export function getJob(project: string, jobId: string): Promise<JobInfo> {
  return request<JobInfo>(`${projectPath(project)}/jobs/${enc(jobId)}`);
}

export function startGroupJob(project: string, body: GroupJobBody): Promise<JobInfo> {
  return post<JobInfo>(`${projectPath(project)}/jobs/group`, body);
}

/** Starting a run can fail with 409 — a scheduler is already driving it. That
 * arrives as an `ApiError` carrying the backend's own explanation, which the
 * form shows verbatim rather than paraphrasing. */
export function startRunJob(project: string, body: RunJobBody): Promise<JobInfo> {
  return post<JobInfo>(`${projectPath(project)}/jobs/run`, body);
}

export function startResumeJob(project: string, body: ResumeJobBody): Promise<JobInfo> {
  return post<JobInfo>(`${projectPath(project)}/jobs/resume`, body);
}

// ---------------------------------------------------------------------- SSE

/** Handlers for a live stream. `onError` fires on transport failure; the
 * `EventSource` retries on its own, so this is a signal, not a teardown. */
export interface StreamHandlers {
  onError?: (event: Event) => void;
}

function streamUrl(path: string, project: string, run: string): string {
  return `${API_BASE}${path}?project=${enc(project)}&run=${enc(run)}`;
}

/** Subscribe to the appended-line log stream (`/events/log`). Each new line
 * arrives as an unnamed message. Returns the `EventSource` — call `.close()`
 * to stop. */
export function openLogStream(
  project: string,
  run: string,
  onLine: (line: string) => void,
  handlers: StreamHandlers = {},
): EventSource {
  const source = new EventSource(streamUrl("/events/log", project, run));
  source.onmessage = (event) => onLine(event.data);
  if (handlers.onError) source.onerror = handlers.onError;
  return source;
}

/** Subscribe to the debounced run-change stream (`/events/run`). Fires the
 * named `changed` event whenever the run directory mutates. Returns the
 * `EventSource` — call `.close()` to stop. */
export function openRunStream(
  project: string,
  run: string,
  onChanged: () => void,
  handlers: StreamHandlers = {},
): EventSource {
  const source = new EventSource(streamUrl("/events/run", project, run));
  source.addEventListener("changed", () => onChanged());
  if (handlers.onError) source.onerror = handlers.onError;
  return source;
}

/** Subscribe to a launched job's log (`/events/job`). Same line-at-a-time shape
 * as `openLogStream`, but keyed by job rather than run — which is what makes a
 * grouping watchable while it runs, before any run directory exists. */
export function openJobStream(
  project: string,
  jobId: string,
  onLine: (line: string) => void,
  handlers: StreamHandlers = {},
): EventSource {
  const source = new EventSource(
    `${API_BASE}/events/job?project=${enc(project)}&job=${enc(jobId)}`,
  );
  source.onmessage = (event) => onLine(event.data);
  if (handlers.onError) source.onerror = handlers.onError;
  return source;
}
