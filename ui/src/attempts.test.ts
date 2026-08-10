// The attempt derivation. Airflow's own history is the argument for these
// tests existing: attempt-history and status-colour components are the first
// surfaces to rot, and every mistake they make looks plausible on screen.

import { describe, expect, it } from "vitest";

import {
  buildAttemptGrid,
  generationsOf,
  isBreakerRetirement,
  isUsageLimitOutage,
  lastWriteMs,
  lostBookkeeping,
  resumeCommand,
  summariseAttempts,
} from "./attempts";
import { R20260726_GROUPING } from "./fixtures/r20260726-grouping";
import { SUPERSEDED_STATUS, statusOf } from "./status";
import type {
  EscalationRequest,
  RunSnapshot,
  SnapshotGroup,
  SnapshotSession,
} from "./types";

function session(over: Partial<SnapshotSession> = {}): SnapshotSession {
  return {
    session_id: "s1",
    role: "coder",
    generation: 1,
    name: "run-g1-coder-g1",
    retirement_reason: null,
    transcript_path: null,
    last_context_tokens: 0,
    rounds_completed: 0,
    total_input_tokens: 0,
    total_output_tokens: 0,
    total_cache_read_tokens: 0,
    total_cache_creation_tokens: 0,
    ...over,
  };
}

function group(over: Partial<SnapshotGroup> = {}): SnapshotGroup {
  return {
    group_id: "g1",
    name: "a-group",
    summary: "",
    state: "completed",
    generation: 1,
    failure: null,
    stale_failure: false,
    depends_on: [],
    sessions: [],
    ...over,
  };
}

function snapshot(groups: SnapshotGroup[], over: Partial<RunSnapshot> = {}): RunSnapshot {
  return {
    project: "smart-mcps",
    run_id: "r-test",
    plan_path: "docs/plans/plan.md",
    groups,
    edges: [],
    stale_dag: false,
    live_pids: {},
    ...over,
  };
}

function escalation(over: Partial<EscalationRequest> = {}): EscalationRequest {
  return {
    id: "e1",
    run_id: "r-test",
    group_id: "g1",
    generation: 1,
    kind: "coder_question",
    prompt: "which schema wins?",
    context: { diff_summary: "", surprises: [] },
    created_at: "2026-08-10T10:00:00Z",
    ...over,
  };
}

function kinds(notes: { kind: string }[]): string[] {
  return notes.map((note) => note.kind);
}

describe("manifest.json is the ground truth for what attempts existed", () => {
  it("renders every generation in the session list, not the state's number", () => {
    // state.json can only say "generation 2, completed". The manifest is what
    // remembers that generation 1 happened at all.
    const g = group({
      group_id: "g2",
      generation: 2,
      state: "completed",
      sessions: [
        session({ session_id: "a", generation: 1, role: "coder" }),
        session({ session_id: "b", generation: 1, role: "reviewer" }),
        session({ session_id: "c", generation: 2, role: "coder" }),
        session({ session_id: "d", generation: 2, role: "reviewer" }),
      ],
    });
    const grid = buildAttemptGrid(snapshot([g]));

    expect(grid.generations).toEqual([1, 2]);
    const [row] = grid.rows;
    expect(row.generationCount).toBe(2);
    expect(row.cells.map((c) => c?.sessions.length)).toEqual([2, 2]);
    // All four attempts survive: the count never passes through state.json.
    expect(row.cells.flatMap((c) => c?.sessions ?? []).map((s) => s.session_id)).toEqual([
      "a",
      "b",
      "c",
      "d",
    ]);
  });

  it("keeps a current generation that has no session written yet", () => {
    const g = group({ generation: 3, state: "running", sessions: [session({ generation: 2 })] });
    expect(generationsOf(g)).toEqual([2, 3]);
  });

  it("colours only the current generation from the state; earlier ones are superseded", () => {
    const g = group({
      generation: 2,
      state: "completed",
      sessions: [session({ generation: 1 }), session({ session_id: "s2", generation: 2 })],
    });
    const [row] = buildAttemptGrid(snapshot([g])).rows;
    expect(row.cells[0]?.status).toBe(SUPERSEDED_STATUS);
    expect(row.cells[1]?.status).toBe(statusOf("completed"));
  });
});

describe("the stale-failure case, on real r20260726-grouping data", () => {
  const grid = buildAttemptGrid(R20260726_GROUPING);
  const rowFor = (id: string) => grid.rows.find((r) => r.group.group_id === id)!;

  it("is not hypothetical: three of the seven groups look like this", () => {
    const stale = R20260726_GROUPING.groups.filter((g) => g.stale_failure);
    expect(stale.map((g) => g.group_id)).toEqual(["g3", "g5", "g6"]);
    for (const g of stale) {
      expect(g.state).toBe("completed");
      expect(g.failure).toBeTruthy();
    }
  });

  it("renders a stale-failure note and a completed colour, never a failure", () => {
    const cell = rowFor("g3").cells[0]!;
    expect(kinds(cell.notes)).toContain("stale_failure");
    // Not the failure style, not the failure label — the group completed.
    expect(cell.status).toBe(statusOf("completed"));
    expect(cell.status.label).toBe("completed");
    expect(cell.status.colour).not.toBe(statusOf("failed").colour);
    // And the note explains itself rather than just showing the string.
    const note = cell.notes.find((n) => n.kind === "stale_failure")!;
    expect(note.detail).toContain("earlier attempt");
    expect(note.detail).toContain("completed");
  });

  it("keeps g2's four sessions across two generations", () => {
    const row = rowFor("g2");
    expect(row.group.sessions).toHaveLength(4);
    expect(row.generationCount).toBe(2);
    expect(row.cells.map((c) => c?.sessions.length)).toEqual([2, 2]);
  });

  it("keeps gen-1's retirement reason intact", () => {
    const cell = rowFor("g2").cells[0]!;
    const retired = cell.sessions.find((s) => s.retirement_reason)!;
    expect(retired.retirement_reason).toBe("context tokens 7618531 exceeded limit 120000");
    expect(kinds(cell.notes)).toContain("breaker_retirement");
  });
});

describe("the seven special cases", () => {
  it("1 — breaker retirement is a property of the conversation, not a verdict", () => {
    expect(isBreakerRetirement("context tokens 7618531 exceeded limit 120000")).toBe(true);
    expect(isBreakerRetirement("superseded by respawn")).toBe(false);
    const g = group({
      sessions: [session({ retirement_reason: "context tokens 7618531 exceeded limit 120000" })],
    });
    const [row] = buildAttemptGrid(snapshot([g])).rows;
    const note = row.cells[0]!.notes.find((n) => n.kind === "breaker_retirement")!;
    expect(note.text).toContain("circuit breaker");
    expect(note.detail).toContain("7618531");
  });

  it("2 — a usage-limit outage is an outage, not a fault in the work", () => {
    expect(isUsageLimitOutage("LlmProcessError: claude -p failed (1): ")).toBe(true);
    expect(isUsageLimitOutage("Claude usage limit reached")).toBe(true);
    expect(isUsageLimitOutage("tests failed")).toBe(false);
    const g = group({
      state: "failed",
      failure: "LlmProcessError: claude -p failed (1): ",
      stale_failure: false,
      sessions: [session()],
    });
    const [row] = buildAttemptGrid(snapshot([g])).rows;
    const note = row.cells[0]!.notes.find((n) => n.kind === "usage_limit_outage")!;
    expect(note.text).toContain("outage");
  });

  it("2b — a stale outage string is history, so it stays under the stale chip", () => {
    // g6 on disk: `completed` with an LlmProcessError string still attached.
    const cell = buildAttemptGrid(R20260726_GROUPING).rows.find(
      (r) => r.group.group_id === "g6",
    )!.cells[0]!;
    expect(kinds(cell.notes)).toContain("stale_failure");
    expect(kinds(cell.notes)).not.toContain("usage_limit_outage");
  });

  it("3 — escalation-blocked is orthogonal to state", () => {
    const g = group({ state: "running", generation: 1, sessions: [session()] });
    const grid = buildAttemptGrid(snapshot([g]), { escalations: [escalation()] });
    const cell = grid.rows[0].cells[0]!;
    expect(kinds(cell.notes)).toContain("escalation_blocked");
    // The state colour is untouched: the group is still running.
    expect(cell.status).toBe(statusOf("running"));
  });

  it("4 — an earlier generation is superseded by a respawn, not failed", () => {
    const g = group({
      generation: 2,
      sessions: [session({ generation: 1 }), session({ session_id: "s2", generation: 2 })],
    });
    const note = buildAttemptGrid(snapshot([g])).rows[0].cells[0]!.notes.find(
      (n) => n.kind === "superseded_by_respawn",
    )!;
    expect(note.text).toBe("superseded by generation 2");
    expect(note.detail).toContain("not a failure");
  });

  it("5 — a self_verify group with no reviewer session is correct, not missing data", () => {
    const g = group({ intensity: "self_verify", sessions: [session({ role: "coder" })] });
    const notes = buildAttemptGrid(snapshot([g])).rows[0].cells[0]!.notes;
    expect(kinds(notes)).toContain("self_verify_no_reviewer");

    const paired = group({ intensity: "paired", sessions: [session({ role: "coder" })] });
    expect(kinds(buildAttemptGrid(snapshot([paired])).rows[0].cells[0]!.notes)).not.toContain(
      "self_verify_no_reviewer",
    );
  });

  it("6 — an interrupted group is resumable, and the command is text", () => {
    const g = group({ state: "interrupted", sessions: [session()] });
    const note = buildAttemptGrid(snapshot([g], { run_id: "r20260726-grouping" })).rows[0]
      .cells[0]!.notes.find((n) => n.kind === "interrupted_resumable")!;
    expect(note.copyable).toBe("smart-mcps-orchestrate resume r20260726-grouping");
    expect(resumeCommand("r-x")).toBe("smart-mcps-orchestrate resume r-x");
  });

  it("7 — a lost round record reads as lost bookkeeping, not as an idle session", () => {
    const worked = session({
      started_at: "2026-08-10T10:00:00Z",
      ended_at: "2026-08-10T11:12:00Z",
      rounds_completed: 0,
      last_context_tokens: 84000,
    });
    expect(lostBookkeeping(worked)).toBe(true);

    const g = group({ state: "interrupted", sessions: [worked] });
    const note = buildAttemptGrid(snapshot([g])).rows[0].cells[0]!.notes.find(
      (n) => n.kind === "bookkeeping_lost",
    )!;
    expect(note.detail).toContain("not an idle session");

    // A finished round is not a loss, and neither is a manifest that predates
    // the fields entirely — that degrades to its own, quieter note.
    expect(lostBookkeeping(session({ started_at: "2026-08-10T10:00:00Z", rounds_completed: 2 }))).toBe(
      false,
    );
    expect(lostBookkeeping(session({ last_context_tokens: 99 }))).toBe(false);
    const old = buildAttemptGrid(R20260726_GROUPING).rows[0].cells[0]!;
    expect(kinds(old.notes)).toContain("actuals_missing");
    expect(kinds(old.notes)).not.toContain("bookkeeping_lost");
  });
});

describe("stalled is an inference", () => {
  const t0 = Date.parse("2026-08-10T12:00:00Z");

  it("states the elapsed fact and never claims the group is hung", () => {
    const g = group({
      state: "running",
      sessions: [session({ transcript_mtime: "2026-08-10T11:37:00Z" })],
    });
    const notes = buildAttemptGrid(snapshot([g]), { nowMs: t0 }).rows[0].cells[0]!.notes;
    const stall = notes.find((n) => n.kind === "stalled")!;
    expect(stall.text).toBe("no activity for 23m");
    expect(JSON.stringify(notes)).not.toMatch(/hung/i);
  });

  it("is not inferred for a group waiting on the operator", () => {
    const g = group({
      state: "running",
      sessions: [session({ transcript_mtime: "2026-08-10T11:00:00Z" })],
    });
    const grid = buildAttemptGrid(snapshot([g]), { nowMs: t0, escalations: [escalation()] });
    expect(kinds(grid.rows[0].cells[0]!.notes)).not.toContain("stalled");
  });

  it("is not inferred for a finished group, however long ago it wrote", () => {
    const g = group({
      state: "completed",
      sessions: [session({ transcript_mtime: "2026-07-01T00:00:00Z" })],
    });
    expect(kinds(buildAttemptGrid(snapshot([g]), { nowMs: t0 }).rows[0].cells[0]!.notes)).not.toContain(
      "stalled",
    );
  });

  it("reads the heartbeat and the transcript mtimes, and takes the latest", () => {
    const g = group({
      heartbeat: {
        generation: 1,
        round: 2,
        started_at: "2026-08-10T10:00:00Z",
        round_started_at: "2026-08-10T11:00:00Z",
        updated_at: "2026-08-10T11:50:00Z",
      },
      sessions: [session({ transcript_mtime: "2026-08-10T11:20:00Z" })],
    });
    expect(lastWriteMs(g)).toBe(Date.parse("2026-08-10T11:50:00Z"));
    expect(lastWriteMs(group())).toBeNull();
  });
});

describe("the board-level signal", () => {
  it("stays quiet for a group with one clean attempt", () => {
    expect(summariseAttempts(group({ sessions: [session()] })).hasHistory).toBe(false);
  });

  it("counts generations and retired sessions from the manifest", () => {
    const g = R20260726_GROUPING.groups.find((x) => x.group_id === "g2")!;
    const summary = summariseAttempts(g);
    expect(summary.hasHistory).toBe(true);
    expect(summary.generations).toBe(2);
    expect(summary.sessions).toBe(4);
    expect(summary.retired).toBe(1);
    expect(summary.label).toBe("2 generations · 1 retired session");
  });
});
