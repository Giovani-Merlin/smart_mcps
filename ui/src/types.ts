// Regenerated from the live Python models — do not hand-drift.
//
// Enums and record shapes mirror `orchestrator/model.py` and
// `orchestrator/execution/scheduler.py`; the `*Snapshot*`, `Project`, `RunInfo`,
// `TranscriptEvent` and `Artifact` types mirror the composed API bodies in
// `orchestrator/observatory/` (runs.py, escalations.py, transcripts.py,
// artifacts.py). Every field name matches its wire form exactly.

// --------------------------------------------------------------- scheduler.py

// GroupState — the ten scheduler-owned lifecycle states (scheduler.py:79).
// `resolved` and `interrupted` arrived with the orchestrator merge; until they
// were added here, an interrupted group rendered an empty, unstyled badge
// because STATE_LABELS had no entry for it.
export type GroupState =
  | "pending"
  | "ready"
  | "running"
  | "reviewing"
  | "rewriting"
  | "merging"
  | "completed"
  | "failed"
  | "resolved"
  | "interrupted";

export const GROUP_STATES: readonly GroupState[] = [
  "pending",
  "ready",
  "running",
  "reviewing",
  "rewriting",
  "merging",
  "completed",
  "failed",
  "resolved",
  "interrupted",
];

export interface GroupRunState {
  state: GroupState;
  generation: number;
  failure?: string | null;
}

export interface RunState {
  run_id: string;
  groups: Record<string, GroupRunState>;
  // pid → session context. Recorded for display only; the read path never
  // checks whether these pids are alive (model.py / scheduler.py:73).
  live_pids: Record<number, string>;
}

// --------------------------------------------------------------------- model.py

export type SessionRole = "base" | "coder" | "reviewer";

// SessionEntry (model.py:68) — flattened into the manifest join.
export interface ManifestSession {
  session_id: string;
  role: SessionRole;
  generation: number;
  name: string;
  retirement_reason?: string | null;
  transcript_path?: string | null;
  // Cost accounting. `last_context_tokens` is the latest round's occupancy —
  // the same quantity the grouper's `estimated_tokens` predicts, and the only
  // honest thing to compare it against. The four cumulative counters sum every
  // round of the session and are a different quantity entirely.
  last_context_tokens: number;
  rounds_completed: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_creation_tokens: number;
  model?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface ManifestGroupEntry {
  group_id: string;
  group_name: string;
  summary: string;
  sessions: ManifestSession[];
}

export interface Manifest {
  run_id: string;
  plan_path: string;
  created_at: string;
  base_session_id?: string | null;
  // The named grouping under `.orchestrator/groupings/<name>/` this run
  // snapshotted; null for a run that predates named groupings.
  grouping?: string | null;
  // The run's persisted HITL configuration.
  escalation?: EscalationConfig | null;
  groups: Record<string, ManifestGroupEntry>;
}

// EscalationConfig (config.py) — the run's HITL tier as it was persisted.
// Without it there is no way to tell a run with escalation switched off from
// one that simply never escalated, and those look identical on the board.
export interface EscalationConfig {
  enabled: boolean;
  intensity: "autonomous" | "on_failure" | "on_stuck" | "interactive";
  source: "orchestrator_only" | "workers_via_orchestrator";
  timeout_s?: number | null;
  on_timeout: "autonomous" | "skip" | "abort";
  poll_interval_s: number;
}

export type SurpriseKind =
  | "interface_mismatch"
  | "missing_dependency"
  | "merge_conflict"
  | "other";

export interface Surprise {
  kind: SurpriseKind;
  description: string;
  affected_groups: string[];
}

export interface VerificationResult {
  item_id: string;
  status: "pass" | "fail" | "skipped";
  notes: string;
}

// CoderReport (model.py) — the parsed content of a `report-*.json` artifact.
//
// `permission_denied` is the typed denial channel: the harness was healthy, a
// sandboxed command was refused, and the coder exhausted its identical-retry
// budget. It routes the group to `interrupted`, never `failed` — the work is
// unfinished, not wrong — so it must not be styled as a failure.
export type CoderReportStatus =
  | "completed"
  | "blocked"
  | "failed"
  | "needs_input"
  | "permission_denied";

export interface CoderReport {
  status: CoderReportStatus;
  summary: string;
  question: string;
  // Verbatim, and required whenever status is `permission_denied` — it is what
  // the operator has to clear before a `resume` gets any further.
  denied_command: string;
  verification_results: VerificationResult[];
  surprises: Surprise[];
}

// ReviewerVerdict (model.py:133) — the parsed content of a `verdict-*.json` artifact.
export interface ReviewerVerdict {
  status: "approved" | "changes_required" | "too_hard" | "structural";
  required_changes: string[];
  surprises: Surprise[];
  notes: string;
}

// EscalationKind — the ten-member enum (model.py:185). The prototype's
// four-value invention had no overlap with these and is gone.
export type EscalationKind =
  | "coder_question"
  | "coder_blocked"
  | "reviewer_too_hard"
  | "reviewer_structural"
  | "merge_conflict"
  | "caps_exhausted"
  | "group_resolve"
  | "group_start"
  | "respawn"
  | "merge_approve";

// HumanAction — the operator's decision (model.py:164).
export type HumanAction = "answer" | "skip" | "abort";

export interface EscalationContext {
  report_path?: string | null;
  verdict_path?: string | null;
  diff_summary: string;
  surprises: Surprise[];
}

// EscalationRequest (model.py:182) — one curated question on the run's channel.
export interface EscalationRequest {
  id: string;
  run_id: string;
  group_id: string;
  generation: number;
  kind: EscalationKind;
  prompt: string;
  context: EscalationContext;
  created_at: string;
}

// -------------------------------------------------------------- observatory API

// Project (registry.py) — one registered repo; `error` set when it is unusable.
export interface Project {
  name: string;
  repo: string;
  error?: string | null;
}

// RunInfo (runs.py) — one entry of a project's run list.
export interface RunInfo {
  run_id: string;
  updated_at?: string | null;
}

// One board card: scheduler state joined to the manifest's group entry (runs.py).
export interface SnapshotSession {
  session_id: string;
  role: string;
  generation: number;
  name: string;
  retirement_reason?: string | null;
  transcript_path?: string | null;
  last_context_tokens: number;
  rounds_completed: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_creation_tokens: number;
  model?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface SnapshotGroup {
  group_id: string;
  name: string;
  summary: string;
  state: GroupState;
  generation: number;
  failure?: string | null;
  // `GroupRunState` is single-valued and last-writer-wins, so a group that
  // failed and was then resolved keeps its old failure string attached to a
  // successful state. When this is set, render a "stale failure text" chip —
  // never a failure. `manifest.json`'s append-only session list is the ground
  // truth for what attempts happened; state.json is authoritative only for now.
  stale_failure: boolean;
  depends_on: string[];
  sessions: SnapshotSession[];
  // From the DAG. `intensity` decides how many reviewer sessions to expect, so
  // a self_verify group with zero of them is correct rather than missing data.
  difficulty?: number | null;
  intensity?: ReviewIntensity | null;
  estimated_tokens?: number | null;
}

export type ReviewIntensity = "self_verify" | "paired" | "paired_plus";

// A dependency edge, in execution order: `from` must complete before `to`.
export interface DagEdge {
  from: string;
  to: string;
}

// RunSnapshot (runs.py) — one body with everything the board renders.
export interface RunSnapshot {
  project: string;
  run_id: string;
  plan_path: string;
  base_session_id?: string | null;
  created_at?: string | null;
  groups: SnapshotGroup[];
  edges: DagEdge[];
  stale_dag: boolean;
  // Display only. Never consult these to decide whether anything is alive — a
  // run whose orchestrator crashed must render exactly like a finished one.
  live_pids: Record<number, string>;
  grouping?: string | null;
  escalation?: EscalationConfig | null;
}

// The POST body for answering an escalation (escalations.py AnswerBody).
export interface AnswerBody {
  action: HumanAction;
  text?: string;
}

// The result of a successful answer (escalations.py AnswerResult).
export interface AnswerResult {
  id: string;
  action: HumanAction;
  answered_at: string;
  response_path: string;
}

// TranscriptEvent (transcripts.py) — one normalized, renderable moment.
export interface TranscriptEvent {
  seq: number;
  role: string; // assistant | user
  kind: string; // text | tool_use | tool_result
  text?: string | null;
  tool_name?: string | null;
  tool_input?: unknown;
  tool_result?: string | null;
  is_error: boolean;
  timestamp?: string | null;
}

// Artifact (artifacts.py) — a parsed `report-*.json` / `verdict-*.json` file.
export interface Artifact {
  name: string;
  kind: string; // report | verdict | other
  content?: unknown;
  error?: string | null;
}
