// The status map is the surface that rots first: a state added to the
// orchestrator and forgotten here renders as a blank badge, which looks like it
// worked. `tsc` catches the union case; these cover what it cannot.

import { describe, expect, it } from "vitest";

import { GROUP_STATES } from "./types";
import type { SnapshotGroup } from "./types";
import {
  ACTIVE_STATES,
  STATUS,
  UNKNOWN_STATUS,
  failureIsCurrent,
  formatDuration,
  inferStall,
  statusOf,
} from "./status";

function group(over: Partial<SnapshotGroup> = {}): SnapshotGroup {
  return {
    group_id: "g1",
    name: "g1",
    summary: "",
    state: "running",
    generation: 1,
    failure: null,
    stale_failure: false,
    depends_on: [],
    sessions: [],
    ...over,
  };
}

describe("status map", () => {
  it("styles every state the orchestrator can produce", () => {
    for (const state of GROUP_STATES) {
      const style = statusOf(state);
      expect(style.label, `${state} has no label`).toBeTruthy();
      expect(style.colour, `${state} has no colour`).toBeTruthy();
      expect(style.glyph, `${state} has no glyph`).toBeTruthy();
    }
    expect(Object.keys(STATUS).sort()).toEqual([...GROUP_STATES].sort());
  });

  it("gives a state from a newer backend a visible badge, not a blank one", () => {
    expect(statusOf("teleported")).toBe(UNKNOWN_STATUS);
    expect(UNKNOWN_STATUS.label).toBe("unknown state");
  });

  it("keeps resolved distinct from completed and from failed", () => {
    expect(statusOf("resolved").colour).not.toBe(statusOf("completed").colour);
    expect(statusOf("resolved").colour).not.toBe(statusOf("failed").colour);
  });

  it("marks interrupted as unfinished without marking it wrong", () => {
    expect(statusOf("interrupted").dashed).toBe(true);
    expect(statusOf("interrupted").colour).not.toBe(statusOf("failed").colour);
  });

  it("collapses the four busy states to one hue told apart by glyph", () => {
    const busy = ACTIVE_STATES.map(statusOf);
    expect(new Set(busy.map((s) => s.colour)).size).toBe(1);
    expect(new Set(busy.map((s) => s.glyph)).size).toBe(busy.length);
  });
});

describe("stale failure text", () => {
  it("does not treat a resolved group's leftover failure as current", () => {
    const resolved = group({ state: "resolved", failure: "reviewer said structural", stale_failure: true });
    expect(failureIsCurrent(resolved)).toBe(false);
  });

  it("does treat an interrupted group's failure as current", () => {
    const interrupted = group({ state: "interrupted", failure: "usage limit reached" });
    expect(failureIsCurrent(interrupted)).toBe(true);
  });
});

describe("stall inference", () => {
  const now = 1_000_000_000;

  it("says nothing about a group that is not active", () => {
    expect(inferStall(group({ state: "completed" }), now - 3_600_000, false, now).stalled).toBe(false);
  });

  it("calls a long-quiet active group stalled, as an observation", () => {
    const result = inferStall(group(), now - 23 * 60_000, false, now);
    expect(result.stalled).toBe(true);
    expect(result.note).toBe("no activity for 23m");
    expect(result.note).not.toMatch(/hung|dead|stuck/);
  });

  it("does not call a group waiting on the operator stalled", () => {
    const result = inferStall(group(), now - 3_600_000, true, now);
    expect(result.stalled).toBe(false);
    expect(result.note).toBe("waiting on the operator");
  });

  it("stays quiet below the threshold", () => {
    expect(inferStall(group(), now - 60_000, false, now).stalled).toBe(false);
  });

  it("formats hours as well as minutes", () => {
    expect(formatDuration(90 * 60_000)).toBe("1h 30m");
  });
});
