// Regenerated from the live Python models — do not hand-drift.
//
// Enums and record shapes mirror `orchestrator/model.py` and
// `orchestrator/execution/scheduler.py`; the `*Snapshot*`, `Project`, `RunInfo`,
// `TranscriptEvent` and `Artifact` types mirror the composed API bodies in
// `orchestrator/observatory/` (runs.py, escalations.py, transcripts.py,
// artifacts.py). Every field name matches its wire form exactly.

// --------------------------------------------------------------- scheduler.py

// GroupState — the eight scheduler-owned lifecycle states (scheduler.py:46).
export type GroupState =
  | "pending"
  | "ready"
  | "running"
  | "reviewing"
  | "rewriting"
  | "merging"
  | "completed"
  | "failed";

export const GROUP_STATES: readonly GroupState[] = [
  "pending",
  "ready",
  "running",
  "reviewing",
  "rewriting",
  "merging",
  "completed",
  "failed",
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
  groups: Record<string, ManifestGroupEntry>;
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

// CoderReport (model.py:110) — the parsed content of a `report-*.json` artifact.
export interface CoderReport {
  status: "completed" | "blocked" | "failed" | "needs_input";
  summary: string;
  question: string;
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

// EscalationKind — the nine-member enum (model.py:145). The prototype's
// four-value invention had no overlap with these and is gone.
export type EscalationKind =
  | "coder_question"
  | "coder_blocked"
  | "reviewer_too_hard"
  | "reviewer_structural"
  | "merge_conflict"
  | "caps_exhausted"
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
}

export interface SnapshotGroup {
  group_id: string;
  name: string;
  summary: string;
  state: GroupState;
  generation: number;
  failure?: string | null;
  depends_on: string[];
  sessions: SnapshotSession[];
}

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
  live_pids: Record<number, string>;
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
