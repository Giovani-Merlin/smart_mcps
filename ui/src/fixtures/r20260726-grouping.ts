// A real run, copied off disk unchanged: `.orchestrator/runs/r20260726-grouping`
// on 2026-08-10, joined exactly as `build_snapshot` joins it — `manifest.json`
// for the sessions, `state.json` for the current state and the failure string.
//
// It is here because every interesting case in the attempt grid is already in
// it, and a hand-written fixture would have quietly omitted the awkward ones:
//
//   g2  4 sessions across 2 generations, gen-1's coder retired by the breaker
//       with "context tokens 7618531 exceeded limit 120000" — the case that
//       proves the grid reads the append-only manifest and not state.json,
//       which reports generation 2 and nothing else.
//   g3  `completed` with a non-null `failure`. So do g5 and g6. This is the
//   g5  stale-failure case, and it is not rare or synthetic — it is three of
//   g6  the seven groups in the first real run anyone looked at.
//   g7  `interrupted`: the resumable case.
//
// The token and timing fields are zeroed the way the backend defaults them for
// a manifest written before per-session actuals existed — which is exactly what
// makes this run the "actuals not recorded" degradation case too.

import type { RunSnapshot } from "../types";

export const R20260726_GROUPING: RunSnapshot = {
  "project": "smart-mcps",
  "run_id": "r20260726-grouping",
  "plan_path": "docs/plans/2026-07-25-001-feat-orchestrator-grouping-improvement-plan.md",
  "base_session_id": "2d43ca10-6b15-4f29-a3ba-a036615cbbcf",
  "created_at": "2026-07-26T13:42:27.356438Z",
  "groups": [
    {
      "group_id": "g1",
      "name": "named-groupings",
      "summary": "Named grouping directories: group --name, run --grouping, groupings listing, and per-run snapshot",
      "state": "completed",
      "generation": 1,
      "failure": null,
      "stale_failure": false,
      "depends_on": [],
      "sessions": [
        {
          "session_id": "3d5b5c1f-d2c0-474a-9d49-7aba756f30e2",
          "role": "coder",
          "generation": 1,
          "name": "r20260726-grouping-g1-coder-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g1-named-groupings/3d5b5c1f-d2c0-474a-9d49-7aba756f30e2.jsonl",
          "last_context_tokens": 18606845,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        },
        {
          "session_id": "2c6c88fd-07f1-4ae3-9738-7b1e5075a422",
          "role": "reviewer",
          "generation": 1,
          "name": "r20260726-grouping-g1-reviewer-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g1-named-groupings/2c6c88fd-07f1-4ae3-9738-7b1e5075a422.jsonl",
          "last_context_tokens": 0,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        }
      ]
    },
    {
      "group_id": "g2",
      "name": "docs-and-skill-lockstep",
      "summary": "Docs, plan skill, and register updated to the new size-hint, slice-invariant, grouping-directory semantics",
      "state": "completed",
      "generation": 2,
      "failure": null,
      "stale_failure": false,
      "depends_on": [],
      "sessions": [
        {
          "session_id": "13be2fe3-71f7-4c78-948a-8d32ac687aa2",
          "role": "coder",
          "generation": 1,
          "name": "r20260726-grouping-g2-coder-g1",
          "retirement_reason": "context tokens 7618531 exceeded limit 120000",
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g2-docs-and-skill-lockstep/13be2fe3-71f7-4c78-948a-8d32ac687aa2.jsonl",
          "last_context_tokens": 7618531,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        },
        {
          "session_id": "47169573-583b-4747-9101-ac18afb111c1",
          "role": "reviewer",
          "generation": 1,
          "name": "r20260726-grouping-g2-reviewer-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g2-docs-and-skill-lockstep/47169573-583b-4747-9101-ac18afb111c1.jsonl",
          "last_context_tokens": 0,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        },
        {
          "session_id": "a309f220-6e03-457b-8cbc-75afa3721545",
          "role": "coder",
          "generation": 2,
          "name": "r20260726-grouping-g2-coder-g2",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g2-docs-and-skill-lockstep/a309f220-6e03-457b-8cbc-75afa3721545.jsonl",
          "last_context_tokens": 489627,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        },
        {
          "session_id": "4a59bba9-9051-4f46-b2d6-1e4f4df35032",
          "role": "reviewer",
          "generation": 2,
          "name": "r20260726-grouping-g2-reviewer-g2",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g2-docs-and-skill-lockstep/4a59bba9-9051-4f46-b2d6-1e4f4df35032.jsonl",
          "last_context_tokens": 0,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        }
      ]
    },
    {
      "group_id": "g3",
      "name": "slice-atoms-hub-independence",
      "summary": "slice_atoms keeps every declared slice member regardless of hub role",
      "state": "completed",
      "generation": 1,
      "failure": "SessionError: claude exited 1 (--session-id 31b218f0-6f70-4751-9c34-4e16aa4961fd): ",
      "stale_failure": true,
      "depends_on": [],
      "sessions": [
        {
          "session_id": "d68a3b33-74ae-4ab7-a210-70609ae764ee",
          "role": "coder",
          "generation": 1,
          "name": "r20260726-grouping-g3-coder-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g3-slice-atoms-hub-independence/d68a3b33-74ae-4ab7-a210-70609ae764ee.jsonl",
          "last_context_tokens": 79965,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        },
        {
          "session_id": "853d6147-ef2b-4b31-bc36-3dbd62b8fefa",
          "role": "reviewer",
          "generation": 1,
          "name": "r20260726-grouping-g3-reviewer-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g3-slice-atoms-hub-independence/853d6147-ef2b-4b31-bc36-3dbd62b8fefa.jsonl",
          "last_context_tokens": 0,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        }
      ]
    },
    {
      "group_id": "g4",
      "name": "partition-core-slices-and-cycles",
      "summary": "Partition core: slice-safe splitter, acyclic merge guard, and SCC repair with dependency-safe re-split",
      "state": "completed",
      "generation": 1,
      "failure": null,
      "stale_failure": false,
      "depends_on": [],
      "sessions": [
        {
          "session_id": "3420b32c-0830-4690-80da-5fa96809dd69",
          "role": "coder",
          "generation": 1,
          "name": "r20260726-grouping-g4-coder-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g4-partition-core-slices-and-cycles/3420b32c-0830-4690-80da-5fa96809dd69.jsonl",
          "last_context_tokens": 332522,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        },
        {
          "session_id": "16ab2f72-1b40-459a-84a7-3881765e68af",
          "role": "reviewer",
          "generation": 1,
          "name": "r20260726-grouping-g4-reviewer-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g4-partition-core-slices-and-cycles/16ab2f72-1b40-459a-84a7-3881765e68af.jsonl",
          "last_context_tokens": 0,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        }
      ]
    },
    {
      "group_id": "g5",
      "name": "slice-overflow-gate",
      "summary": "Slice overflow fails loudly naming the slice, with --allow-oversized-slice keeping it whole",
      "state": "completed",
      "generation": 1,
      "failure": "SessionError: claude exited 1 (--session-id 4ce1d9b7-3726-4e3a-8468-ba33199a601e): ",
      "stale_failure": true,
      "depends_on": [],
      "sessions": [
        {
          "session_id": "2d1e14b3-b78a-406f-946b-4367b7bac29b",
          "role": "coder",
          "generation": 1,
          "name": "r20260726-grouping-g5-coder-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g5-slice-overflow-gate/2d1e14b3-b78a-406f-946b-4367b7bac29b.jsonl",
          "last_context_tokens": 208024,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        },
        {
          "session_id": "fb73e6e1-40e5-4ff8-914f-00123be65f32",
          "role": "reviewer",
          "generation": 1,
          "name": "r20260726-grouping-g5-reviewer-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g5-slice-overflow-gate/fb73e6e1-40e5-4ff8-914f-00123be65f32.jsonl",
          "last_context_tokens": 0,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        }
      ]
    },
    {
      "group_id": "g6",
      "name": "size-hints",
      "summary": "size_hints prices prospective files at small/medium/large = 500/2000/5000",
      "state": "completed",
      "generation": 1,
      "failure": "LlmProcessError: claude -p failed (1): ",
      "stale_failure": true,
      "depends_on": [],
      "sessions": [
        {
          "session_id": "a34ad8ba-e3c7-4eca-bf61-844257b42dc6",
          "role": "coder",
          "generation": 1,
          "name": "r20260726-grouping-g6-coder-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g6-size-hints/a34ad8ba-e3c7-4eca-bf61-844257b42dc6.jsonl",
          "last_context_tokens": 148378,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        },
        {
          "session_id": "4d5187c0-53da-40ee-b1b3-89f063b9cb9e",
          "role": "reviewer",
          "generation": 1,
          "name": "r20260726-grouping-g6-reviewer-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g6-size-hints/4d5187c0-53da-40ee-b1b3-89f063b9cb9e.jsonl",
          "last_context_tokens": 0,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        }
      ]
    },
    {
      "group_id": "g7",
      "name": "grouping-trace",
      "summary": "Versioned grouping trace: recorder across all stages plus the artifact written in every group mode",
      "state": "interrupted",
      "generation": 1,
      "failure": "SessionError: claude exited 1 (--resume a9155653-0097-489b-9a70-0eb69ad07b07): ",
      "stale_failure": false,
      "depends_on": [],
      "sessions": [
        {
          "session_id": "a9155653-0097-489b-9a70-0eb69ad07b07",
          "role": "coder",
          "generation": 1,
          "name": "r20260726-grouping-g7-coder-g1",
          "retirement_reason": null,
          "transcript_path": "/home/gbm1996/.claude/projects/-home-gbm1996-wksp-smart-mcps--worktrees-g7-grouping-trace/a9155653-0097-489b-9a70-0eb69ad07b07.jsonl",
          "last_context_tokens": 427680,
          "rounds_completed": 0,
          "total_input_tokens": 0,
          "total_output_tokens": 0,
          "total_cache_read_tokens": 0,
          "total_cache_creation_tokens": 0
        }
      ]
    }
  ],
  "edges": [],
  "stale_dag": false,
  "live_pids": {}
};
