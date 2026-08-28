// A synthetic run in the new manifest format — the one shape no run on disk has
// yet, because every existing manifest predates the token-class split.
//
// It is built to hold the cases the cost tab has to get right, and each group
// exists for one of them:
//
//   g1  `paired`: one coder and one reviewer session, both with all four
//       counters and the coder carrying a per-round `rounds` list. Cache reads
//       are ~80% of its spend — the healthy shape, and the one a chart must not
//       render as "this group was expensive".
//   g2  `self_verify`: zero reviewer sessions, which is *correct* for the
//       intensity and must read as expected rather than as missing data.
//   g3  no `intensity` at all, the older-`groups.json` case: the expected
//       reviewer count is unknown and says so instead of assuming one.
//   g4  an estimate with no coder occupancy recorded — excluded from the
//       calibration rollup, and named as excluded.
//
// The `rounds` on g1's coder sum exactly to its cumulative counters, which is
// what lets a test assert the totals are per-round sums rather than anything
// read off a session-level envelope.

import type { RunSnapshot } from "../types";

export const COST_NEW_FORMAT: RunSnapshot = {
  project: "smart-mcps",
  run_id: "r20260810-cost",
  plan_path: "docs/plans/2026-08-08-observatory-grouping-provenance-and-attempt-history.md",
  base_session_id: "8f1c2d90-0000-4000-8000-00000000base",
  created_at: "2026-08-10T09:00:00Z",
  stale_dag: false,
  live_pids: {},
  edges: [{ from: "g1", to: "g2" }],
  groups: [
    {
      group_id: "g1",
      name: "instrumentation",
      summary: "Grouper call records and the session token-class split",
      state: "completed",
      generation: 1,
      failure: null,
      stale_failure: false,
      depends_on: [],
      difficulty: 6.2,
      intensity: "paired",
      estimated_tokens: 83_215,
      sessions: [
        {
          session_id: "c0000000-0000-4000-8000-000000000001",
          role: "coder",
          generation: 1,
          name: "r20260810-cost/g1/coder/1",
          transcript_path: "/repo/.claude/projects/x/c1.jsonl",
          last_context_tokens: 96_400,
          rounds_completed: 3,
          // = the `rounds` below, summed. Deliberately equal, so a test can
          // prove the panel adds rounds up rather than reading a total.
          total_input_tokens: 12_000,
          total_output_tokens: 24_000,
          total_cache_read_tokens: 480_000,
          total_cache_creation_tokens: 60_000,
          base_context_tokens: 90_000,
          model: "claude-opus-5",
          started_at: "2026-08-10T09:01:00Z",
          ended_at: "2026-08-10T09:48:00Z",
          rounds: [
            {
              input_tokens: 6_000,
              output_tokens: 9_000,
              cache_read_input_tokens: 90_000,
              cache_creation_input_tokens: 40_000,
            },
            {
              input_tokens: 4_000,
              output_tokens: 8_000,
              cache_read_input_tokens: 180_000,
              cache_creation_input_tokens: 14_000,
            },
            {
              input_tokens: 2_000,
              output_tokens: 7_000,
              cache_read_input_tokens: 210_000,
              cache_creation_input_tokens: 6_000,
            },
          ],
        },
        {
          session_id: "r0000000-0000-4000-8000-000000000001",
          role: "reviewer",
          generation: 1,
          name: "r20260810-cost/g1/reviewer/1",
          transcript_path: "/repo/.claude/projects/x/r1.jsonl",
          last_context_tokens: 41_200,
          rounds_completed: 2,
          total_input_tokens: 5_000,
          total_output_tokens: 9_000,
          total_cache_read_tokens: 120_000,
          total_cache_creation_tokens: 22_000,
          model: "claude-opus-5",
          started_at: "2026-08-10T09:20:00Z",
          ended_at: "2026-08-10T09:47:00Z",
        },
      ],
    },
    {
      group_id: "g2",
      name: "ui-cost-panel",
      summary: "Estimate vs actual, four token classes, honest degradation",
      state: "completed",
      generation: 1,
      failure: null,
      stale_failure: false,
      depends_on: ["g1"],
      difficulty: 2.1,
      intensity: "self_verify",
      estimated_tokens: 89_932,
      sessions: [
        {
          session_id: "c0000000-0000-4000-8000-000000000002",
          role: "coder",
          generation: 1,
          name: "r20260810-cost/g2/coder/1",
          transcript_path: null,
          last_context_tokens: 71_000,
          rounds_completed: 2,
          total_input_tokens: 8_000,
          total_output_tokens: 16_000,
          total_cache_read_tokens: 240_000,
          total_cache_creation_tokens: 30_000,
          model: "claude-opus-5",
          started_at: "2026-08-10T10:00:00Z",
          ended_at: "2026-08-10T10:35:00Z",
        },
      ],
    },
    {
      group_id: "g3",
      name: "legacy-intensity",
      summary: "A group whose groups.json predates the intensity field",
      state: "completed",
      generation: 1,
      failure: null,
      stale_failure: false,
      depends_on: [],
      estimated_tokens: 40_000,
      sessions: [
        {
          session_id: "c0000000-0000-4000-8000-000000000003",
          role: "coder",
          generation: 1,
          name: "r20260810-cost/g3/coder/1",
          transcript_path: null,
          last_context_tokens: 52_000,
          rounds_completed: 1,
          total_input_tokens: 3_000,
          total_output_tokens: 6_000,
          total_cache_read_tokens: 90_000,
          total_cache_creation_tokens: 11_000,
          model: "claude-opus-5",
          started_at: "2026-08-10T11:00:00Z",
          ended_at: "2026-08-10T11:12:00Z",
        },
      ],
    },
    {
      group_id: "g4",
      name: "never-started",
      summary: "Estimated, never run",
      state: "pending",
      generation: 1,
      failure: null,
      stale_failure: false,
      depends_on: ["g1"],
      difficulty: 3.0,
      intensity: "paired_plus",
      estimated_tokens: 55_000,
      sessions: [],
    },
    {
      group_id: "g5",
      name: "multi-generation-example",
      summary: "Burned four coder generations before one landed",
      state: "completed",
      generation: 4,
      failure: null,
      stale_failure: false,
      depends_on: [],
      difficulty: 7.4,
      intensity: "paired",
      estimated_tokens: 50_000,
      sessions: [
        {
          session_id: "c0000000-0000-4000-8000-000000000005",
          role: "coder",
          generation: 1,
          name: "r20260810-cost/g5/coder/1",
          transcript_path: null,
          retirement_reason: "breaker retired: context 80000 exceeded limit 78000",
          last_context_tokens: 80_000, // the peak — this generation was retired
          rounds_completed: 2,
          total_input_tokens: 4_000,
          total_output_tokens: 8_000,
          total_cache_read_tokens: 60_000,
          total_cache_creation_tokens: 8_000,
          model: "claude-opus-5",
          started_at: "2026-08-10T12:00:00Z",
          ended_at: "2026-08-10T12:20:00Z",
        },
        {
          session_id: "c0000000-0000-4000-8000-000000000006",
          role: "coder",
          generation: 2,
          name: "r20260810-cost/g5/coder/2",
          transcript_path: null,
          retirement_reason: "merge conflict: preflight regression",
          last_context_tokens: 40_000,
          rounds_completed: 1,
          total_input_tokens: 2_000,
          total_output_tokens: 4_000,
          total_cache_read_tokens: 30_000,
          total_cache_creation_tokens: 4_000,
          model: "claude-opus-5",
          started_at: "2026-08-10T12:25:00Z",
          ended_at: "2026-08-10T12:35:00Z",
        },
        {
          session_id: "c0000000-0000-4000-8000-000000000007",
          role: "coder",
          generation: 3,
          name: "r20260810-cost/g5/coder/3",
          transcript_path: null,
          retirement_reason: "re-entry fallback: session unreachable",
          last_context_tokens: 60_000,
          rounds_completed: 1,
          total_input_tokens: 2_500,
          total_output_tokens: 5_000,
          total_cache_read_tokens: 35_000,
          total_cache_creation_tokens: 5_000,
          model: "claude-opus-5",
          started_at: "2026-08-10T12:40:00Z",
          ended_at: "2026-08-10T12:50:00Z",
        },
        {
          session_id: "c0000000-0000-4000-8000-000000000008",
          role: "coder",
          generation: 4,
          name: "r20260810-cost/g5/coder/4",
          transcript_path: null,
          // no retirement_reason — the generation that finally landed
          last_context_tokens: 45_000, // the last generation — below the peak
          rounds_completed: 2,
          total_input_tokens: 2_000,
          total_output_tokens: 4_000,
          total_cache_read_tokens: 32_000,
          total_cache_creation_tokens: 4_500,
          model: "claude-opus-5",
          started_at: "2026-08-10T12:55:00Z",
          ended_at: "2026-08-10T13:10:00Z",
        },
      ],
    },
  ],
};
