// Mirrors orchestrator/execution/scheduler.py and orchestrator/model.py.

export type GroupState =
  | "pending"
  | "ready"
  | "running"
  | "reviewing"
  | "rewriting"
  | "merging"
  | "completed"
  | "failed";

export interface GroupRunState {
  state: GroupState;
  generation: number;
  failure?: string | null;
}

export interface RunState {
  run_id: string;
  groups: Record<string, GroupRunState>;
}

export interface ManifestSession {
  name: string;
  role: string;
}

export interface ManifestGroupEntry {
  group_id: string;
  group_name: string;
  sessions: ManifestSession[];
}

export interface Manifest {
  run_id: string;
  base_session_id: string | null;
  groups: Record<string, ManifestGroupEntry>;
}

export type EscalationKind = "too_hard" | "structural" | "blocked" | "needs_input";

export interface Escalation {
  id: string;
  kind: EscalationKind;
  group_id: string;
  prompt: string;
}
