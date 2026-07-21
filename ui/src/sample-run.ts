import type { Escalation, Manifest, RunState } from "./types";

export const sampleRunState: RunState = {
  run_id: "smoke1",
  groups: {
    g1: { state: "completed", generation: 1 },
    g2: { state: "completed", generation: 1 },
    g3: { state: "reviewing", generation: 1 },
    g4: { state: "rewriting", generation: 2, failure: "reviewer requested changes" },
    g5: { state: "running", generation: 1 },
    g6: { state: "ready", generation: 1 },
    g7: { state: "pending", generation: 1 },
    g8: { state: "failed", generation: 3, failure: "TypeError: undefined is not a function" },
  },
};

export const sampleManifest: Manifest = {
  run_id: "smoke1",
  base_session_id: "smoke1-base",
  groups: {
    g1: {
      group_id: "g1",
      group_name: "types-sample-and-views",
      sessions: [{ name: "smoke1-g1-coder-g1", role: "coder" }],
    },
    g2: {
      group_id: "g2",
      group_name: "scaffold-vite-project",
      sessions: [{ name: "smoke1-g2-coder-g1", role: "coder" }],
    },
    g3: {
      group_id: "g3",
      group_name: "orchestrator-api-client",
      sessions: [
        { name: "smoke1-g3-coder-g1", role: "coder" },
        { name: "smoke1-g3-reviewer-g1", role: "reviewer" },
      ],
    },
    g4: {
      group_id: "g4",
      group_name: "escalation-modal",
      sessions: [
        { name: "smoke1-g4-coder-g1", role: "coder" },
        { name: "smoke1-g4-reviewer-g1", role: "reviewer" },
        { name: "smoke1-g4-coder-g2", role: "coder" },
      ],
    },
    g5: {
      group_id: "g5",
      group_name: "run-selector",
      sessions: [{ name: "smoke1-g5-coder-g1", role: "coder" }],
    },
    g6: {
      group_id: "g6",
      group_name: "keyboard-shortcuts",
      sessions: [],
    },
    g7: {
      group_id: "g7",
      group_name: "docs-and-readme",
      sessions: [],
    },
    g8: {
      group_id: "g8",
      group_name: "log-tailing",
      sessions: [
        { name: "smoke1-g8-coder-g1", role: "coder" },
        { name: "smoke1-g8-coder-g2", role: "coder" },
        { name: "smoke1-g8-coder-g3", role: "coder" },
      ],
    },
  },
};

export const sampleEventLog: string[] = [
  "[12:00:01] run smoke1 started (8 groups)",
  "[12:00:01] g1 pending -> ready",
  "[12:00:01] g2 pending -> ready",
  "[12:00:02] g1 ready -> running (coder session smoke1-g1-coder-g1)",
  "[12:00:02] g2 ready -> running (coder session smoke1-g2-coder-g1)",
  "[12:04:18] g2 running -> completed",
  "[12:04:20] g5 pending -> ready (dependency g2 completed)",
  "[12:04:21] g5 ready -> running (coder session smoke1-g5-coder-g1)",
  "[12:05:47] g1 running -> completed",
  "[12:05:50] g3 pending -> ready (dependency g1 completed)",
  "[12:05:51] g3 ready -> running (coder session smoke1-g3-coder-g1)",
  "[12:09:03] g3 running -> reviewing (reviewer session smoke1-g3-reviewer-g1)",
  "[12:11:12] g4 running -> reviewing (reviewer session smoke1-g4-reviewer-g1)",
  "[12:12:40] g4 reviewing -> rewriting (changes_required, generation 2)",
  "[12:15:09] g8 running -> failed: TypeError: undefined is not a function",
  "[12:15:09] escalation raised for g8 (too_hard)",
];

export const sampleEscalations: Escalation[] = [
  {
    id: "esc-1",
    kind: "too_hard",
    group_id: "g8",
    prompt:
      "g8 failed after 3 generations with the same TypeError in the log-tailing writer. " +
      "Should the run retry with a narrower spec, or should log tailing be dropped from this plan?",
  },
];
