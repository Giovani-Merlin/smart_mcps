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

// RoundUsage (execution/sessions.py:68) — one round's four token classes, as
// parsed from that round's CLI envelope.
//
// The cumulative counters on a session are the sum of these, round by round.
// They are emphatically *not* the envelope's own top-level `usage`, which sums
// every turn of the session and once produced a 50x-inflated context reading
// that retired healthy coders. Anything cumulative in the cost panels is built
// by adding these up; nothing re-reads a session-level total.
export interface RoundUsage {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
}

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
  // Per-round history, when the manifest carries it. No orchestrator on disk
  // writes this yet — A3 shipped the four cumulative counters without the
  // `rounds` list the plan suggested — so absence is the normal case and the
  // sparkline simply does not render. Present or absent, the cumulative
  // figures stay a sum of per-round values.
  rounds?: RoundUsage[] | null;
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
  // When the transcript file was last appended to — the cheapest evidence that
  // a session is still producing anything, recorded for free by the runner.
  // Null when the path is unset or the file is already gone. This and the
  // group's heartbeat are the two inputs to the stall *inference*; there is no
  // stalled field on the wire and there must not be one.
  transcript_mtime?: string | null;
  // Per-round history when the manifest carries it; see `ManifestSession`.
  rounds?: RoundUsage[] | null;
}

// GroupHeartbeat (runs.py) — `heartbeat.json` passed through unchanged.
//
// Facts only: when this round started, which round it is, when the writer last
// ran. No "stalled" field, deliberately — persisting the inference would make
// it a de facto state that later code branches on. The UI computes staleness
// from these numbers and says that it is doing so.
export interface GroupHeartbeat {
  started_at?: string | null;
  generation: number;
  round: number;
  round_started_at?: string | null;
  updated_at?: string | null;
  // What the group is doing between round boundaries. Rounds cannot explain a
  // silence that happens before round 1 exists, and that is the longest one:
  // forking the base session took 21 minutes on a real run. Null for every run
  // written before the phase shipped.
  phase?: string | null;
  phase_elapsed_s?: number | null;
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
  // Null for every run written before the heartbeat shipped, and for any group
  // that never started a round. Absence is normal, never an error.
  heartbeat?: GroupHeartbeat | null;
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

// PathEntry (paths.py) — one `PathChip`'s worth of on-disk location. `root` and
// `rel` are null for a path that is deliberately not fetchable; the chip still
// shows it, because the operator's next move is to open it in an editor.
export interface PathEntry {
  key: string;
  label: string;
  panel: string;
  path: string;
  kind: "file" | "directory" | string;
  exists: boolean;
  root?: string | null;
  rel?: string | null;
  description?: string;
}

// RunPathsView (paths.py) — every file-backed panel source in the run, listed
// whether or not it exists: a missing artifact is the entry whose path the
// operator most wants.
export interface RunPathsView {
  project: string;
  run_id: string;
  roots: Record<string, string>;
  entries: PathEntry[];
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

// EventUsage (transcripts.py) — one assistant turn's token usage, four classes
// kept apart. Cache reads are the cheap class: a session whose spend is mostly
// cache-read is healthy, not expensive, and should be de-emphasised visually.
export interface EventUsage {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
}

export type TranscriptKind =
  | "text"
  | "thinking"
  | "redacted_thinking"
  | "tool_use"
  | "tool_result";

// TranscriptEvent (transcripts.py) — one normalized, renderable moment.
//
// `seq` counts emitted events from the start of the file, so it is stable
// across a full fetch and an `?after_seq=` incremental one — which is what
// makes a `?seq=` deep link keep pointing at the same turn.
export interface TranscriptEvent {
  seq: number;
  role: string; // assistant | user
  kind: TranscriptKind;
  text?: string | null;
  tool_name?: string | null;
  tool_input?: unknown;
  tool_result?: string | null;
  is_error: boolean;
  timestamp?: string | null;
  // Present on assistant rows; null on user rows and on transcripts written
  // before usage was recorded. Null and "all four are zero" are different
  // claims and the UI must not conflate them.
  usage?: EventUsage | null;
  model?: string | null;
  // A thinking block whose prose the transcript did not keep — which is every
  // thinking block in every transcript observed so far. Render the card with
  // the marker, so "the agent thought here" stays distinguishable from "the
  // agent went straight to the next tool call".
  thinking_withheld: boolean;
}

// Artifact (artifacts.py) — a parsed `report-*.json` / `verdict-*.json` file.
export interface Artifact {
  name: string;
  kind: string; // report | verdict | other
  content?: unknown;
  error?: string | null;
}

// ------------------------------------------------------------- grouping tab

// Mirrors `orchestrator/observatory/grouping.py`. Everything except
// `stage_diffs` is `grouping-trace.json` passed through unchanged, so the trace
// on disk and what the tab shows can never disagree.

export type DagSourceKind =
  | "run_snapshot"
  | "named_grouping"
  | "shared_fallback"
  | "missing";

// Where the DAG was resolved from. `stale_dag` means exactly what it means on
// the board — this run has no frozen groups.json of its own — and resolving a
// better source than the shared file does not change that.
export interface DagSource {
  kind: DagSourceKind;
  directory?: string | null;
  groups_path?: string | null;
  grouping_name?: string | null;
  stale_dag: boolean;
  reason: string;
}

// An artifact the tab wanted and did not find. `expected_path` is the point:
// the operator's next move is to go look there.
export interface MissingArtifact {
  artifact: string;
  expected_path: string;
  explanation: string;
}

// What changed between two consecutive pipeline stages. `moved` is computed
// from co-membership, not group ids — `renumber` relabels every group without
// moving anything, and an id diff would light up the whole graph.
export interface StageDiff {
  stage: string;
  previous_stage?: string | null;
  moved: string[];
  added: string[];
  removed: string[];
  group_count: number;
}

/** One `stages[]` entry: the whole partition as it stood after that stage. */
export interface StageSnapshot {
  stage: string;
  partition: Record<string, number>;
}

export interface HubRole {
  node: string;
  role: string;
  depends_on_ratio: number;
  depended_by_ratio: number;
  threshold: number;
}

export interface SliceAtom {
  label: string;
  members: string[];
}

export interface LouvainEntry {
  resolution: number;
  seed: number;
  communities: string[][];
}

export interface MergeCandidate {
  round: number;
  source: number;
  target: number;
  accepted: boolean;
  reason: string;
  merged_work: number;
  edge_weight: number;
}

export interface Scorecard {
  group_count: number;
  cross_group_edges: number;
  work_fraction_min: number;
  work_fraction_mean: number;
  work_fraction_max: number;
  critical_path_length: number;
  modularity: number;
  slice_integrity_ok: boolean;
}

export interface GroupingProvenance {
  timestamp: string;
  plan_path: string;
  plan_content_sha256: string;
  repo_commit_sha: string;
  worktree_dirty: boolean;
  index_fingerprint: string;
}

/** `[from, to, weight]`, as the trace stores it. */
export type WeightedEdge = [string, string, number];

export interface GraphSnapshot {
  nodes: string[];
  affinity: WeightedEdge[];
  dependencies: WeightedEdge[];
}

export interface NodeWork {
  node: string;
  source_bytes: number;
  file_count: number;
  bytes_tokens: number;
  file_allowance_tokens: number;
  total: number;
}

export interface GroupDifficulty {
  group_id: string;
  files_touched: number;
  max_fan_in: number;
  max_fan_out: number;
  hub_touches: number;
  cross_group_edges: number;
  verification_items: number;
  difficulty: number;
  intensity: ReviewIntensity;
  d_review: number;
  d_hard: number;
}

// GroupingView (grouping.py) — one body with everything the tab renders.
export interface GroupingView {
  project: string;
  run_id: string;
  plan_path: string;
  dag_source: DagSource;
  missing: MissingArtifact[];
  trace_path?: string | null;
  trace_schema_version?: number | null;
  trace_schema_known: boolean;
  input_graph?: GraphSnapshot | null;
  node_work: NodeWork[];
  budget?: Record<string, number> | null;
  config: Record<string, unknown>;
  hub_roles: HubRole[];
  slice_atoms: SliceAtom[];
  stages: StageSnapshot[];
  louvain: LouvainEntry[];
  splits: Record<string, unknown>[];
  merges: MergeCandidate[];
  repairs: Record<string, unknown>[];
  group_difficulty: GroupDifficulty[];
  scorecard?: Scorecard | null;
  provenance?: GroupingProvenance | null;
  last_stage?: string | null;
  flags: string[];
  mapper_flags: string[];
  partition_flags: string[];
  failure?: { kind: string; message: string } | null;
  stage_diffs: StageDiff[];
  // Not written by any orchestrator on disk yet; null means "look in `missing`
  // for where it was expected", not "there is no provenance".
  edge_provenance?: Record<string, unknown> | null;
  paths: Record<string, string>;
}
