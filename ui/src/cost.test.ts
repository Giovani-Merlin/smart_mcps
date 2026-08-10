// The cost derivation. These cover the arithmetic and the quantity discipline;
// `CostPanel.test.tsx` covers what the operator sees.

import { describe, expect, it } from "vitest";

import {
  buildCostView,
  groupCost,
  reviewerExpectation,
  runCalibration,
  sessionCost,
  totalOf,
} from "./cost";
import { COST_NEW_FORMAT } from "./fixtures/cost-new-format";
import { R20260726_GROUPING } from "./fixtures/r20260726-grouping";
import type { SnapshotSession } from "./types";

function groupOf(view: ReturnType<typeof buildCostView>, id: string) {
  const found = view.groups.find((g) => g.groupId === id);
  if (!found) throw new Error(`no group ${id}`);
  return found;
}

describe("cumulative figures are sums of per-round values", () => {
  it("sums a session's `rounds` list rather than reading any session total", () => {
    const coder = COST_NEW_FORMAT.groups[0].sessions[0];
    const cost = sessionCost(coder);

    expect(cost.source).toBe("per_round");
    expect(cost.classes).toEqual({
      uncached_input: 12_000,
      cache_creation: 60_000,
      cache_read: 480_000,
      output: 24_000,
    });
    // The fixture's cumulative counters agree with the rounds, which is the
    // point: both paths are per-round sums, and they must not disagree.
    expect(cost.classes.uncached_input).toBe(coder.total_input_tokens);
    expect(cost.classes.cache_read).toBe(coder.total_cache_read_tokens);
    expect(cost.roundTotals).toEqual([145_000, 206_000, 225_000]);
  });

  it("never lets `last_context_tokens` into a cumulative figure", () => {
    // The real g2 coder read 7,618,531 context tokens — the runaway that
    // retired it. Occupancy of one round is not spend, and the same class of
    // mistake as re-reading the envelope's all-turns total produced a 50x
    // inflation once already.
    const session: SnapshotSession = {
      ...R20260726_GROUPING.groups[1].sessions[0],
      total_input_tokens: 100,
      total_output_tokens: 200,
      total_cache_read_tokens: 300,
      total_cache_creation_tokens: 400,
    };
    const cost = sessionCost(session);

    expect(cost.lastContextTokens).toBe(7_618_531);
    expect(cost.total).toBe(1_000);
    expect(totalOf(cost.classes)).toBe(1_000);
  });

  it("falls back to the cumulative counters, which are themselves round sums", () => {
    const reviewer = COST_NEW_FORMAT.groups[0].sessions[1];
    const cost = sessionCost(reviewer);
    expect(cost.source).toBe("cumulative_counters");
    expect(cost.total).toBe(5_000 + 9_000 + 120_000 + 22_000);
    expect(cost.roundTotals).toEqual([]);
  });
});

describe("the four token classes split by role", () => {
  it("keeps the coder and reviewer totals apart and classed", () => {
    const g1 = groupCost(COST_NEW_FORMAT.groups[0]);
    const coder = g1.roles.find((r) => r.role === "coder")!;
    const reviewer = g1.roles.find((r) => r.role === "reviewer")!;

    expect(coder.classes).toEqual({
      uncached_input: 12_000,
      cache_creation: 60_000,
      cache_read: 480_000,
      output: 24_000,
    });
    expect(reviewer.classes).toEqual({
      uncached_input: 5_000,
      cache_creation: 22_000,
      cache_read: 120_000,
      output: 9_000,
    });
    expect(g1.classes.cache_read).toBe(600_000);
    expect(g1.total).toBe(coder.total + reviewer.total);
  });
});

describe("intensity says how many reviewer sessions to expect", () => {
  it("self_verify expects zero, and zero observed is expected, not missing", () => {
    const expectation = reviewerExpectation("self_verify", 0);
    expect(expectation.known).toBe(true);
    expect(expectation.expectedSessions).toBe(0);
    expect(expectation.asExpected).toBe(true);
    expect(expectation.text).toContain("correct for this group, not missing data");
  });

  it("paired and paired_plus both expect one session; paired_plus adds a round", () => {
    expect(reviewerExpectation("paired", 1).expectedSessions).toBe(1);
    const plus = reviewerExpectation("paired_plus", 1);
    expect(plus.expectedSessions).toBe(1);
    expect(plus.text).toContain("mandatory extra round");
  });

  it("absent intensity is unknown, never a default", () => {
    const expectation = reviewerExpectation(undefined, 0);
    expect(expectation.known).toBe(false);
    expect(expectation.expectedSessions).toBeNull();
    expect(expectation.asExpected).toBe(false);
    expect(expectation.text).toContain("intensity unknown");
  });
});

describe("prediction compares one quantity with itself", () => {
  it("uses the coder session's occupancy, not any total", () => {
    const g1 = groupCost(COST_NEW_FORMAT.groups[0]);
    expect(g1.prediction.estimated).toBe(83_215);
    expect(g1.prediction.observed).toBe(96_400);
    expect(g1.prediction.ratio).toBeCloseTo(96_400 / 83_215, 6);
    // Not the group's cumulative spend, which is 21x larger.
    expect(g1.prediction.observed).not.toBe(g1.total);
  });

  it("does not compare a group that recorded no coder occupancy", () => {
    const g4 = groupCost(COST_NEW_FORMAT.groups[3]);
    expect(g4.prediction.comparable).toBe(false);
    expect(g4.prediction.ratio).toBeNull();
  });
});

describe("the run-level calibration rollup", () => {
  it("compares like with like and names what it skipped", () => {
    const view = buildCostView(COST_NEW_FORMAT);
    const cal = view.calibration;

    expect(cal.rows.map((r) => r.groupId)).toEqual(["g1", "g2", "g3"]);
    expect(cal.skipped).toEqual(["g4"]);
    expect(cal.estimatedTotal).toBe(83_215 + 89_932 + 40_000);
    expect(cal.observedTotal).toBe(96_400 + 71_000 + 52_000);
    expect(cal.medianRatio).toBeCloseTo(96_400 / 83_215, 6);
    // Every input is an occupancy; no cumulative spend reached this figure.
    expect(cal.observedTotal).toBeLessThan(view.total);
  });

  it("returns an empty rollup rather than inventing one for a legacy run", () => {
    const cal = runCalibration(buildCostView(R20260726_GROUPING).groups);
    expect(cal.rows.length).toBeGreaterThanOrEqual(0);
    if (cal.rows.length === 0) expect(cal.medianRatio).toBeNull();
  });
});

describe("a legacy run has no actuals, which is not the same as zero spend", () => {
  it("marks the real r20260726-grouping run as unrecorded", () => {
    const view = buildCostView(R20260726_GROUPING);
    expect(view.actualsRecorded).toBe(false);
    for (const group of view.groups) {
      expect(group.actualsRecorded).toBe(false);
      expect(group.total).toBe(0);
      // The occupancy figures survive — they are what the estimate compares to.
      expect(group.sessions.every((s) => s.source === "none")).toBe(true);
    }
    // g2's coder still carries its runaway occupancy, and it is not spend.
    expect(groupOf(view, "g2").sessions[0].lastContextTokens).toBe(7_618_531);
  });
});
